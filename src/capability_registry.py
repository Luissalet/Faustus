"""
capability_registry.py — which backends exist, what they can do, and what we
actually know about them right now.

The catalogue came first on purpose (Phase 0), so that the router and the
backends of Phase 1 had something to agree with instead of growing side by
side and discovering they disagreed.

The distinction the module is built around comes from Diogenes: *definitions
are durable intent, observations are disposable facts*.  A declaration says
`docker_workspace` isolates in a container; that is a promise about the design.
An observation says whether anything answered just now — and when nothing was
asked, the answer is `unknown`, never `available`.

Nothing is rounded up, and the three ways a backend can be unusable stay
apart, because each has a different fix: `not_implemented` (write the code),
`unavailable` (start the daemon, pull the image) and `attended_only` (say yes
on the run). A registry that collapsed them would send a run somewhere it
cannot start, and the run would be blamed for it.
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
        implemented=True,
        note="Unprivileged uid 1000, one workspace mounted read-write, network "
             "denied unless the manifest asked for it, dropped capabilities, "
             "memory/CPU/pid limits, and a timeout that kills the container. "
             "Never pulls an image on its own.",
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

#: A probe costs a subprocess and a round trip to the daemon, and a page that
#: lists four backends would pay it four times per refresh. The cache is short
#: and every Observation carries the `checked_at` it was taken at, so a stale
#: answer is visible rather than implied.
_PROBE_TTL_S = 10.0
_probe_cache: Dict[str, Tuple[float, Observation]] = {}


def observe(backend_id: str, *, fresh: bool = False) -> Observation:
    """What can be said about this backend *right now*.

    Three states, and the middle one earns its keep: `unknown` means nobody
    asked, which is not the same as asking and being told no. Nothing here is
    rounded up — a `docker` binary on PATH is evidence about the machine, and
    the daemon is asked separately."""
    stamp = now_iso()
    decl = _BY_ID.get(backend_id)
    if decl is None:
        return Observation(backend_id, "unavailable", "no such backend is declared", stamp)
    if not decl.implemented:
        return Observation(backend_id, "unavailable",
                           "declared but not implemented in this build", stamp)
    if backend_id == "local":
        return Observation(backend_id, "available", "this process", stamp)

    if not fresh:
        cached = _probe_cache.get(backend_id)
        if cached and (time.monotonic() - cached[0]) < _PROBE_TTL_S:
            return cached[1]

    if backend_id == "docker_workspace":
        observation = _probe_docker(stamp)
    else:
        observation = Observation(backend_id, "unknown", "no probe implemented yet", stamp)
    _probe_cache[backend_id] = (time.monotonic(), observation)
    return observation


def _probe_docker(stamp: str) -> Observation:
    """Ask the backend itself rather than re-deriving what "ready" means here.

    A missing image is `unavailable`, not `available with a caveat`: a run sent
    to a backend that cannot start it is a run that fails for a reason its
    output will not explain."""
    from src.execution_backends import DockerWorkspaceBackend

    try:
        gate = DockerWorkspaceBackend().probe()
    except Exception as e:                       # a probe never takes the page down
        return Observation("docker_workspace", "unknown",
                           f"the probe itself failed: {e}", stamp)
    if gate["ok"]:
        return Observation("docker_workspace", "available", gate["detail"], stamp)
    return Observation("docker_workspace", "unavailable",
                       f"{gate['reason']}: {gate['detail']}", stamp)


def observe_all(*, fresh: bool = False) -> Tuple[Observation, ...]:
    return tuple(observe(d.id, fresh=fresh) for d in DECLARATIONS)


def docker_evidence() -> Dict[str, Any]:
    """Deliberately separate from `observe`.  Finding the `docker` binary is
    evidence about the *machine*; whether the backend can take work is a
    different question, and `observe("docker_workspace")` is the one that asks
    the daemon and the image. Kept apart so a UI can show "you have Docker
    installed but it is not running" instead of one flat "unavailable"."""
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
    "binary": "filesystem",
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
        elif obs.state != "available":
            # Built, and not answering. Kept apart from `not_implemented`
            # because one of them is fixed by writing code and the other by
            # starting a daemon, and a router that conflated them would send
            # the run somewhere it cannot start.
            row.update(ok=False, reason="unavailable", detail=obs.evidence)
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
    order = {"unavailable": 0, "not_implemented": 1, "attended_only": 2,
             "missing_capability": 3, "not_requested": 4, "not_declared": 5}
    rows.sort(key=lambda r: order.get(r["reason"], 9))
    nearest = rows[0]
    return (f"no backend can run {manifest.id} {manifest.version}: closest is "
            f"{nearest['backend']} ({nearest['reason']}" +
            (f" — {nearest['detail']}" if nearest["detail"] else "") + ")")


def check_spec(spec: ExecutionSpec, permissions: Permissions) -> Dict[str, Any]:
    """Does this spec stay inside both the declaration and the manifest?

    `execution_router` calls it on its own output — the router is not exempt
    from the rule it enforces — and it is also what catches a spec built by
    hand somewhere else.

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
