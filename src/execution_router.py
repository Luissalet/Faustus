"""
execution_router.py — capability → backend, and the one thing it will not do.

The router turns a `SkillManifest` into an `ExecutionSpec` and picks the
backend that can honour it. Its whole reason to exist as its own module is a
single rule that is easy to state and easy to lose in a call site:

    **The host is never a fallback.**

Not when Docker is down, not when the image is missing, not when the container
took too long. `local` is reachable only when the caller passes an explicit
acknowledgement *and* the manifest named it — two independent yeses, neither
of which a failure elsewhere can supply. A router that quietly degrades to the
host is a router that turns an outage into an unsandboxed run, and the person
who reads the log tomorrow will see a successful run and no sign of what it
actually ran on.

The second rule is quieter and does as much work: the spec the router builds
is derived from the manifest's permissions, so it can only ever be *narrower*.
It then checks its own output with `capability_registry.check_spec` before
handing it over — the router does not get to trust itself either.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import artifact_store
from src import capability_registry as registry
from src import execution_backends
from src.contracts import ExecutionResult, ExecutionSpec, SkillManifest
from src.contracts.base import now_iso


@dataclass(frozen=True)
class Decision:
    """Why this backend, or why none. `candidates` is always the full list —
    "it picked the slow one" and "it picked nothing" are both questions the
    caller should be able to answer without re-deriving the reasoning."""

    ok: bool
    backend: str = ""
    spec: Optional[ExecutionSpec] = None
    reason: str = ""
    detail: str = ""
    candidates: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "backend": self.backend,
            "spec": self.spec.to_dict() if self.spec else None,
            "reason": self.reason, "detail": self.detail,
            "candidates": [dict(c) for c in self.candidates],
        }


def artifacts_dir_for(artifacts_root: str, run_id: str, *, create: bool = True) -> str:
    """One directory per run. This is what makes "the run finds nothing in
    /artifacts" true rather than aspirational — see the note in
    `execution_backends`, where a shared directory once credited a container
    with the previous container's file.

    Delegates to the store so there is one naming rule, not two that agree
    until someone edits one of them. `create=False` is for asking what would
    happen without leaving a directory behind for a run that never ran."""
    if create:
        return artifact_store.run_dir(run_id, root=artifacts_root)
    return os.path.join(artifacts_root, artifact_store.run_slug(run_id))


def build_spec(manifest: SkillManifest, backend_id: str, *, workspace: str,
               artifacts_dir: str, attended_ack: bool = False) -> ExecutionSpec:
    """The narrowest spec that still lets the manifest do what it declared.

    Every field is *derived* from the permissions rather than passed in, so
    there is no argument a caller can supply that widens the run. The one
    exception is `attended_ack`, which cannot be derived because it is the
    human's, not the manifest's."""
    decl = registry.declaration(backend_id)
    perms = manifest.permissions
    seconds = perms.max_seconds or (decl.max_seconds_default if decl else None)
    return ExecutionSpec.parse({
        "backend": backend_id,
        "isolation": decl.isolation if decl else "container",
        "workspace": workspace,
        "artifacts_dir": artifacts_dir,
        "network": perms.network,
        "network_allowlist": list(perms.network_allowlist),
        "secret_names": list(perms.secrets),
        "limits": {"seconds": seconds, "cost_units": perms.max_cost_units},
        "attended_ack": bool(attended_ack),
    })


def choose(manifest: SkillManifest, *, workspace: str, artifacts_root: str,
           run_id: str, attended_ack: bool = False,
           prefer: Optional[str] = None, create_dirs: bool = True) -> Decision:
    """Pick a backend, or refuse and say what the nearest miss was.

    `create_dirs=False` answers the same question without leaving a scratch
    directory behind — what a "what would you do?" endpoint needs."""
    rows = tuple(registry.candidates(manifest))
    artifacts_dir = artifacts_dir_for(artifacts_root, run_id, create=create_dirs)
    eligible = [r["backend"] for r in rows if r["ok"]]

    # The host, when and only when both yeses are present. This is written
    # before the eligibility loop on purpose: it must be reachable by an
    # explicit request and unreachable by everything else.
    if attended_ack and "local" in manifest.permissions.backends:
        return _verified(manifest, "local", workspace, artifacts_dir, rows, attended_ack=True)

    if prefer and prefer in eligible:
        return _verified(manifest, prefer, workspace, artifacts_dir, rows)
    if prefer and prefer not in eligible:
        row = next((r for r in rows if r["backend"] == prefer), None)
        return Decision(
            ok=False, reason="preferred_backend_unusable",
            detail=(f"{prefer} cannot take this run ({row['reason']}"
                    + (f" — {row['detail']}" if row and row.get("detail") else "") + "). "
                    "Not falling back: a run that lands somewhere other than where it was "
                    "sent is a run nobody can reason about.") if row else
                   f"{prefer} is not a declared backend",
            candidates=rows)

    for backend_id in eligible:
        if backend_id == "local":
            continue                      # never by default, only by the branch above
        return _verified(manifest, backend_id, workspace, artifacts_dir, rows)

    return Decision(ok=False, reason="no_backend",
                    detail=registry.why_no_backend(manifest)
                           or "no declared backend can run this manifest",
                    candidates=rows)


def _verified(manifest: SkillManifest, backend_id: str, workspace: str,
              artifacts_dir: str, rows: Tuple[Dict[str, Any], ...], *,
              attended_ack: bool = False) -> Decision:
    """Build the spec, then check it against the same registry that would have
    caught someone else's spec. The router is not exempt from its own rule."""
    spec = build_spec(manifest, backend_id, workspace=workspace,
                      artifacts_dir=artifacts_dir, attended_ack=attended_ack)
    verdict = registry.check_spec(spec, manifest.permissions)
    if not verdict["ok"]:
        return Decision(
            ok=False, backend=backend_id, spec=spec, reason="spec_rejected",
            detail="; ".join(f"{p['field']}: {p['detail']}" for p in verdict["problems"]),
            candidates=rows)
    return Decision(ok=True, backend=backend_id, spec=spec,
                    reason="eligible", candidates=rows)


def execute(manifest: SkillManifest, command: Any, *, workspace: str,
            artifacts_root: str, run_id: str, attended_ack: bool = False,
            prefer: Optional[str] = None,
            secrets: Optional[Dict[str, str]] = None,
            image: Optional[str] = None,
            on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
            ) -> Tuple[Decision, Optional[ExecutionResult]]:
    """Choose, then run. Returns both so a caller can report *why* a run went
    where it went, not only what came back."""
    decision = choose(manifest, workspace=workspace, artifacts_root=artifacts_root,
                      run_id=run_id, attended_ack=attended_ack, prefer=prefer)
    if not decision.ok:
        if on_event:
            on_event("tool.blocked", {"reason": decision.reason, "detail": decision.detail})
        return decision, None

    kwargs: Dict[str, Any] = {}
    if image and decision.backend == "docker_workspace":
        kwargs["image"] = image
    backend = execution_backends.build(decision.backend, **kwargs)

    granted = {name: (secrets or {}).get(name) for name in decision.spec.secret_names}
    missing = sorted(name for name, value in granted.items() if value is None)
    if missing:
        # Refuse rather than start a run that will fail deep inside for a
        # reason the output will not explain.
        return decision, ExecutionResult.parse({
            "run_id": run_id, "backend": decision.backend, "status": "refused",
            "reason": f"policy: the spec declares secrets {missing} and no value was "
                      "supplied for them",
            "started_at": now_iso(), "ended_at": now_iso(),
        })

    result = backend.run(decision.spec, command, run_id=run_id,
                         secrets=granted, on_event=on_event)
    return decision, result
