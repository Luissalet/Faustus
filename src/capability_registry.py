"""
capability_registry.py — which backends exist, what they can do, and what we
actually know about them right now.

Read-only on purpose.  Phase 0 of the masterplan asks for a catalogue before
an executor, so that Phase 1 has something to route against instead of growing
a router and a backend at the same time and discovering they disagree.

The distinction the module is built around comes from Diogenes: *definitions
are durable intent, observations are disposable facts*.  A declaration says
`docker_workspace` isolates in a container; that is a promise about the design.
An observation says whether anything answered just now — and when nothing was
asked, the answer is `unknown`, never `available`.

The honest middle state matters more than it sounds.  `docker` on PATH proves a
CLI is installed, not that a daemon is running, so the probe reports
`cli_present` as its evidence and leaves the state `unknown`.  A registry that
rounded that up to "available" would send the first real run into a timeout and
blame the run.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts import ExecutionSpec, Permissions, SkillManifest
from src.contracts.base import now_iso

#: What a backend can be asked to do.  A capability is a promise the router can
#: check a manifest against, so the list is closed and boring on purpose.
CAPABILITIES = (
    "shell",            # run a command line
    "python",           # run Python in the workspace
    "filesystem",       # read/write the mounted workspace
    "documents",        # produce docx/pdf/md
    "image",            # generate or edit images
    "video",            # render or transcode video
    "audio",            # synthesise or transcode audio
    "gpu",              # can be given a GPU
    "host",             # can touch the machine outside a workspace
)

STATES = ("available", "unavailable", "unknown")


@dataclass(frozen=True)
class BackendDeclaration:
    """Durable intent.  None of this is measured; all of it is designed."""

    id: str
    title: str
    isolation: str                      # none | process | container | remote
    capabilities: Tuple[str, ...]
    artifact_kinds: Tuple[str, ...] = ()
    network_default: bool = False
    attended_only: bool = False
    max_seconds_default: Optional[int] = None
    implemented: bool = False           # is there code behind it yet?
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "isolation": self.isolation,
            "capabilities": list(self.capabilities),
            "artifact_kinds": list(self.artifact_kinds),
            "network_default": self.network_default,
            "attended_only": self.attended_only,
            "max_seconds_default": self.max_seconds_default,
            "implemented": self.implemented, "note": self.note,
        }


@dataclass(frozen=True)
class Observation:
    """Disposable fact.  `evidence` is what was actually seen, in words — the
    field exists so nobody has to guess what `unknown` was based on."""

    backend_id: str
    state: str = "unknown"
    evidence: str = "not probed"
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"backend_id": self.backend_id, "state": self.state,
                "evidence": self.evidence, "checked_at": self.checked_at}


# ── the catalogue ──────────────────────────────────────────────────────────
#
# `implemented=False` on everything but `local` is the truthful state of the
# repository today, and the registry says so out loud rather than listing four
# backends as if any of them could take work.

DECLARATIONS: Tuple[BackendDeclaration, ...] = (
    BackendDeclaration(
        id="local",
        title="This machine, with the user watching",
        isolation="none",
        capabilities=("shell", "python", "filesystem", "documents", "host"),
        artifact_kinds=("document", "code", "text", "json", "dataset"),
        network_default=True,
        attended_only=True,
        implemented=True,
        note="Not a sandbox and never described as one. Reachable only with an "
             "explicit acknowledgement on the run, never by falling back from a "
             "backend that was unavailable.",
    ),
    BackendDeclaration(
        id="docker_workspace",
        title="Container with one mounted workspace",
        isolation="container",
        capabilities=("shell", "python", "filesystem", "documents"),
        artifact_kinds=("document", "code", "text", "json", "dataset", "archive"),
        network_default=False,
        max_seconds_default=900,
        implemented=False,
        note="Phase 1. Unprivileged user, one workspace mounted read-write, "
             "network denied by default, /artifacts write-only.",
    ),
    BackendDeclaration(
        id="media_worker",
        title="GPU worker for image, video and audio",
        isolation="process",
        capabilities=("image", "video", "audio", "gpu", "filesystem"),
        artifact_kinds=("image", "video", "audio", "json"),
        network_default=False,
        max_seconds_default=3600,
        implemented=False,
        note="Phase 3. ComfyUI over its API as a separate service, plus ffmpeg. "
             "Queued, cancellable, and out of the web process.",
    ),
    BackendDeclaration(
        id="remote_worker",
        title="A paired machine",
        isolation="remote",
        capabilities=("shell", "python", "filesystem", "gpu"),
        artifact_kinds=("document", "code", "text", "json", "image", "video"),
        network_default=False,
        max_seconds_default=3600,
        implemented=False,
        note="Phase 6. Opt-in, paired and revocable.",
    ),
)

_BY_ID: Dict[str, BackendDeclaration] = {d.id: d for d in DECLARATIONS}


def declarations() -> Tuple[BackendDeclaration, ...]:
    return DECLARATIONS


def declaration(backend_id: str) -> Optional[BackendDeclaration]:
    return _BY_ID.get(backend_id)


# ── observations ───────────────────────────────────────────────────────────

def observe(backend_id: str) -> Observation:
    """What can be said about this backend *right now*, cheaply and honestly.

    Nothing here starts a container or opens a socket; those belong to Phase 1.
    What it can do is tell apart "there is no code for this yet" from "there is
    code and something is missing" — and refuse to call a CLI on PATH a running
    daemon."""
    stamp = now_iso()
    decl = _BY_ID.get(backend_id)
    if decl is None:
        return Observation(backend_id, "unavailable", "no such backend is declared", stamp)
    if not decl.implemented:
        return Observation(backend_id, "unavailable",
                           "declared but not implemented in this build", stamp)
    if backend_id == "local":
        return Observation(backend_id, "available", "this process", stamp)
    return Observation(backend_id, "unknown", "no probe implemented yet", stamp)


def observe_all() -> Tuple[Observation, ...]:
    return tuple(observe(d.id) for d in DECLARATIONS)


def docker_evidence() -> Dict[str, Any]:
    """Deliberately separate from `observe`.  Finding the `docker` binary is
    evidence about the machine, not about the backend: the daemon may be
    stopped, the socket may be unreachable, and Phase 1 is what will find out.
    Kept here so the Phase 1 probe has one place to grow from."""
    path = shutil.which("docker")
    return {
        "cli_present": bool(path),
        "path": path or None,
        "means": "a CLI on PATH does not prove a daemon is running",
        "checked_at": now_iso(),
    }


# ── what a manifest needs, and who could give it ───────────────────────────

_KIND_TO_CAPABILITY = {
    "image": "image", "video": "video", "audio": "audio",
    "document": "documents", "code": "filesystem", "dataset": "filesystem",
    "text": "filesystem", "json": "filesystem", "archive": "filesystem",
}


def required_capabilities(manifest: SkillManifest) -> Tuple[str, ...]:
    """Read the requirement off the manifest instead of asking it to repeat
    itself: a skill that declares an `artifact:video` output needs a backend
    that can make video, whether or not it also said so."""
    needed = {_KIND_TO_CAPABILITY[kind] for kind in manifest.output_kinds()
              if kind in _KIND_TO_CAPABILITY}
    if manifest.permissions.host_access:
        needed.add("host")
    if manifest.permissions.filesystem != "none":
        needed.add("filesystem")
    return tuple(sorted(needed))


def candidates(manifest: SkillManifest) -> List[Dict[str, Any]]:
    """Every declared backend, with a verdict and a reason.  Backends that
    cannot take the work stay in the list: "why did it not pick the GPU one"
    is the question this answers."""
    needed = set(required_capabilities(manifest))
    named = set(manifest.permissions.backends)
    out: List[Dict[str, Any]] = []

    for missing_id in sorted(named - set(_BY_ID)):
        out.append({"backend": missing_id, "ok": False, "reason": "not_declared",
                    "detail": "the manifest names a backend this build does not know",
                    "missing": [], "observed": observe(missing_id).to_dict()})

    for decl in DECLARATIONS:
        obs = observe(decl.id)
        row: Dict[str, Any] = {"backend": decl.id, "observed": obs.to_dict(),
                               "missing": sorted(needed - set(decl.capabilities))}
        if named and decl.id not in named:
            row.update(ok=False, reason="not_requested",
                       detail=f"the manifest limits itself to {sorted(named)}")
        elif row["missing"]:
            row.update(ok=False, reason="missing_capability",
                       detail=f"cannot provide {row['missing']}")
        elif decl.attended_only:
            row.update(ok=False, reason="attended_only",
                       detail="reachable only with an explicit acknowledgement on the run")
        elif not decl.implemented:
            row.update(ok=False, reason="not_implemented",
                       detail=decl.note or "declared but not built yet")
        else:
            row.update(ok=True, reason="eligible", detail="")
        out.append(row)
    return out


def why_no_backend(manifest: SkillManifest) -> str:
    """One sentence a user can read when nothing is eligible.  It names the
    nearest miss rather than saying "no backend available", which is true and
    useless."""
    rows = candidates(manifest)
    if any(r["ok"] for r in rows):
        return ""
    order = {"not_implemented": 0, "attended_only": 1, "missing_capability": 2,
             "not_requested": 3, "not_declared": 4}
    rows.sort(key=lambda r: order.get(r["reason"], 9))
    nearest = rows[0]
    return (f"no backend can run {manifest.id} {manifest.version}: closest is "
            f"{nearest['backend']} ({nearest['reason']}" +
            (f" — {nearest['detail']}" if nearest["detail"] else "") + ")")


def check_spec(spec: ExecutionSpec, permissions: Permissions) -> Dict[str, Any]:
    """The read-only half of what the Phase 1 router will do: does this spec
    stay inside both the declaration and the manifest?

    A spec may always be *narrower* than the permissions.  Anything wider is
    reported field by field — an approval was given for what the manifest
    said, so a spec that quietly adds a secret is a different question."""
    decl = _BY_ID.get(spec.backend)
    problems: List[Dict[str, str]] = []
    if decl is None:
        problems.append({"field": "backend", "detail": "no such backend is declared"})
    else:
        if spec.isolation != decl.isolation:
            problems.append({
                "field": "isolation",
                "detail": f"spec says {spec.isolation!r}, {decl.id} provides "
                          f"{decl.isolation!r}; the spec does not get to relabel it",
            })
        if decl.attended_only and not spec.attended_ack:
            problems.append({"field": "attended_ack",
                             "detail": f"{decl.id} runs on the host and needs it"})
    for excess in spec.grants_beyond(permissions):
        problems.append({"field": "permissions", "detail": f"grants {excess}, "
                                                           "which the manifest did not ask for"})
    return {"ok": not problems, "backend": spec.backend, "problems": problems}
