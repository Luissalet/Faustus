"""
doctor.py — what this machine can actually do, asked rather than assumed.

Six phases of the masterplan each added a probe, and each probe is honest on
its own: the capability registry asks Docker, the media backend asks ComfyUI,
`workspace_checkpoints` asks git. What was missing is the one place that asks
them all and answers the question a person actually has, which is never "is
the docker daemon up" but **"why did that not work, and what do I do?"**

Three rules, and they are the same three the rest of the platform runs on:

**Nothing reports OK that was not checked.** A check that could not run comes
back `unknown` with the reason, never `ok`. An `unknown` rounded up to `ok` is
how somebody spends an evening on a feature that was never going to work.

**Every finding that is not OK carries the fix.** "docker: unavailable" sends
someone to a search engine. "docker: the CLI is installed but the daemon did
not answer — start Docker Desktop" sends them to the taskbar. If a check
cannot name a fix, that is a gap in the check.

**A missing capability is not a fault.** No ComfyUI on this machine is a fact
about the machine, not a broken install, and it is reported as `absent` rather
than `fail`. Painting every unused capability red teaches people to ignore the
report, which costs more than the report is worth.

It is pure enough to run from a CLI with the app stopped: every probe is
wrapped, and one that raises becomes an `unknown` finding rather than a
traceback.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: Worst to best. `absent` is deliberately not `fail`: a capability nobody
#: installed is a fact about the machine.
STATES = ("fail", "unknown", "absent", "warn", "ok")

_RANK = {name: i for i, name in enumerate(STATES)}


@dataclass(frozen=True)
class Finding:
    """One thing that was asked, and what it answered."""

    area: str
    name: str
    state: str
    detail: str = ""
    fix: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"area": self.area, "name": self.name, "state": self.state,
                "detail": self.detail, "fix": self.fix, "facts": dict(self.facts)}


def _check(area: str, name: str) -> Callable:
    """Wrap a probe so it can never take the report down with it.

    A doctor that crashes on the machine it was written to diagnose is worse
    than no doctor: the one time it matters is the one time something is
    broken enough to raise."""
    def wrap(fn: Callable[[], Finding]) -> Callable[[], Finding]:
        def run() -> Finding:
            try:
                return fn()
            except Exception as e:
                return Finding(area, name, "unknown",
                               f"the check itself failed: {type(e).__name__}: {e}",
                               fix="this is a bug in the check, not in the machine")
        return run
    return wrap


# ── the machine itself ────────────────────────────────────────────────────

def _python() -> Finding:
    version = ".".join(str(p) for p in sys.version_info[:3])
    ok = sys.version_info >= (3, 11)
    return Finding(
        "runtime", "python", "ok" if ok else "fail",
        f"{version} on {platform.system()} {platform.release()}",
        fix="" if ok else "Faustus needs Python 3.11 or newer",
        facts={"version": version, "executable": sys.executable,
               "venv": sys.prefix != sys.base_prefix})


def _git() -> Finding:
    path = shutil.which("git")
    if not path:
        return Finding("runtime", "git", "absent",
                       "no git on PATH",
                       fix="install Git — without it there are no checkpoints, "
                           "so no diff and no way to undo a turn")
    return Finding("runtime", "git", "ok", path, facts={"path": path})


def _data_dir() -> Finding:
    from src.constants import DATA_DIR

    if not os.path.isdir(DATA_DIR):
        return Finding("runtime", "data directory", "fail",
                       f"{DATA_DIR} does not exist",
                       fix="run setup.py, or point ODYSSEUS_DATA_DIR at the right "
                           "folder — a Faustus with the wrong data directory looks "
                           "empty rather than broken")
    writable = os.access(DATA_DIR, os.W_OK)
    try:
        free_gb = shutil.disk_usage(DATA_DIR).free / (1024 ** 3)
    except Exception:
        free_gb = None
    state = "ok" if writable else "fail"
    detail = DATA_DIR + (f" · {free_gb:.1f} GB free" if free_gb is not None else "")
    if writable and free_gb is not None and free_gb < 2:
        state, detail = "warn", detail + " — renders and checkpoints need room"
    return Finding("runtime", "data directory", state, detail,
                   fix="" if writable else f"{DATA_DIR} is not writable by this user",
                   facts={"path": DATA_DIR, "free_gb": free_gb})


# ── the backends the contracts declare ────────────────────────────────────

def _backends() -> List[Finding]:
    from src import capability_registry as registry

    found: List[Finding] = []
    for declaration in registry.DECLARATIONS:
        observed = registry.observe(declaration.id, fresh=True)
        if not declaration.implemented:
            found.append(Finding(
                "backends", declaration.id, "absent",
                f"{declaration.title} — declared, not built in this version",
                fix="nothing to do; it is on the roadmap",
                facts={"isolation": declaration.isolation}))
            continue
        state = {"available": "ok", "unavailable": "warn",
                 "unknown": "unknown"}[observed.state]
        found.append(Finding(
            "backends", declaration.id, state,
            f"{declaration.title} — {observed.evidence}",
            fix=_backend_fix(declaration.id, observed.state),
            facts={"isolation": declaration.isolation,
                   "capabilities": list(declaration.capabilities)}))
    return found


def _backend_fix(backend_id: str, state: str) -> str:
    if state == "available":
        return ""
    return {
        "docker_workspace": "start Docker, then build the sandbox image with "
                            "scripts/build_sandbox_image.ps1 — Faustus never "
                            "pulls or builds an image on its own",
        "media_worker": "run D:\\LocalAI\\Start-ComfyUI.ps1 (or start ComfyUI "
                        "however you keep it) and make sure a checkpoint is in "
                        "its models/checkpoints folder; point COMFYUI_URL at it "
                        "if it is not on http://127.0.0.1:8188",
        "local": "",
    }.get(backend_id, "")


# ── what each phase actually needs ────────────────────────────────────────

def _checkpoints() -> Finding:
    from src import workspace_checkpoints

    if not workspace_checkpoints.enabled():
        return Finding("coding", "checkpoints", "absent",
                       "switched off in settings",
                       fix="turn on `agent_checkpoints` — without them a turn has "
                           "no diff, so nothing can check what it claims")
    if not workspace_checkpoints.git_available():
        return Finding("coding", "checkpoints", "fail",
                       "git is not available, so no checkpoint can be made",
                       fix="install Git")
    return Finding("coding", "checkpoints", "ok",
                   "a shadow git repo per workspace, outside the user's own repo")


def _tests_runner() -> Finding:
    from src import project_tests

    detect = getattr(project_tests, "detect_test_command", None)
    if detect is None:
        return Finding("coding", "test runner", "unknown",
                       "project_tests has no detector in this build")
    return Finding("coding", "test runner", "ok",
                   "detected per workspace when a turn ends",
                   facts={"note": "a workspace with no test command makes a turn "
                                  "unverified, not failed"})


def _media_engines() -> Finding:
    """Every ComfyUI this Faustus knows about, not just the first.

    On a machine with two GPUs the useful fact is usually "one of them is
    down", and a check that stopped at the first engine would report a healthy
    half as a healthy whole."""
    from src.media_backends import pool

    engines = pool.survey()
    ready = [e for e in engines if e.ok]
    if not engines:
        return Finding("media", "engines", "absent", "none configured",
                       fix="set COMFYUI_URL, or COMFYUI_URLS for more than one")
    lines = "; ".join(
        f"{e.url} {'ok' if e.ok else e.reason}"
        + (f" [{e.gpu}{f', {e.vram_gb} GB' if e.vram_gb else ''}"
           f"{f', {e.queued} queued' if e.queued is not None else ''}]" if e.ok else "")
        for e in engines)
    if not ready:
        return Finding("media", "engines", "warn",
                       f"0 of {len(engines)} answering — {lines}",
                       fix=_backend_fix("media_worker", "unavailable"),
                       facts={"engines": [e.to_dict() for e in engines]})
    state = "ok" if len(ready) == len(engines) else "warn"
    return Finding("media", "engines", state,
                   f"{len(ready)} of {len(engines)} ready — {lines}",
                   fix="" if state == "ok" else
                       "one engine is not answering; renders still work on the "
                       "others, just with less to go round",
                   facts={"engines": [e.to_dict() for e in engines]})


def _media_templates() -> Finding:
    from src import media_workflows

    found = media_workflows.catalogue()
    broken = found["broken"]
    count = len(found["workflows"])
    if broken:
        return Finding("media", "templates", "fail",
                       f"{count} usable, {len(broken)} that will not parse: "
                       + "; ".join(f"{b['file']} ({b['field']})" for b in broken[:3]),
                       fix="fix the named field — a broken template is invisible to "
                           "the model, which reads as 'it refuses to use it'",
                       facts={"directory": found["directory"]})
    if not count:
        return Finding("media", "templates", "absent",
                       f"no templates in {found['directory']}",
                       fix="a render can only use an approved template; without one "
                           "the media engine has nothing to run")
    return Finding("media", "templates", "ok",
                   ", ".join(f"{w.id} {w.version}" for w in found["workflows"]),
                   facts={"directory": found["directory"], "count": count})


def _sandbox_image() -> Finding:
    from src import execution_backends

    image = getattr(execution_backends, "DEFAULT_IMAGE", "")
    backend = execution_backends.DockerWorkspaceBackend()
    gate = backend.probe()
    if gate["ok"]:
        return Finding("execution", "sandbox image", "ok", f"{image} is present")
    if gate.get("reason") == "image_missing":
        return Finding("execution", "sandbox image", "warn", gate["detail"],
                       fix=f"build it: scripts/build_sandbox_image.ps1 — Faustus "
                           f"never builds or pulls {image} on its own")
    return Finding("execution", "sandbox image", "unknown",
                   f"could not be checked: {gate.get('detail', '')}",
                   fix="the daemon has to answer before the image can be asked about")


def _agent_sandbox() -> Finding:
    from src import sandbox_exec

    on = sandbox_exec.enabled()
    return Finding("execution", "agent shell in the sandbox",
                   "ok" if on else "absent",
                   "bash and python run in a container" if on else
                   "OFF — the agent's shell runs on this machine, as it always did",
                   fix="" if on else "turn on `agent_sandbox_execution` once you "
                                     "have used it a while on the test instance; it "
                                     "adds ~0.4s per command and breaks anything "
                                     "that needs host tools the image lacks")


def _approvals() -> Finding:
    from src import approval_store

    approval_store.expire_stale()
    pending = approval_store.pending(limit=50)
    if not pending:
        return Finding("approvals", "pending cards", "ok", "nothing is waiting")
    return Finding("approvals", "pending cards", "warn",
                   f"{len(pending)} card(s) waiting on a person: "
                   + ", ".join(f"{c.id} ({c.plan.action})" for c in pending[:3]),
                   fix="answer them in the UI; a run that raised one is parked "
                       "until somebody does",
                   facts={"count": len(pending)})


def _workflows() -> Finding:
    from core.database import SessionLocal, WorkflowRunRow

    db = SessionLocal()
    try:
        paused = (db.query(WorkflowRunRow)
                  .filter(WorkflowRunRow.status == "paused").count())
        running = (db.query(WorkflowRunRow)
                   .filter(WorkflowRunRow.status.in_(("running", "pending"))).count())
    finally:
        db.close()
    if not (paused or running):
        return Finding("workflows", "runs", "ok", "nothing in flight")
    return Finding(
        "workflows", "runs", "warn",
        f"{paused} paused, {running} still going",
        fix="nothing calls advance() on a timer yet, so a paused run waits until "
            "somebody asks — POST /api/workflows/runs/{id}/advance",
        facts={"paused": paused, "running": running})


def _media_runs() -> Finding:
    from core.database import MediaRunRow, SessionLocal

    db = SessionLocal()
    try:
        open_runs = (db.query(MediaRunRow)
                     .filter(MediaRunRow.status.in_(("pending", "queued", "running")))
                     .count())
    finally:
        db.close()
    if not open_runs:
        return Finding("media", "renders", "ok", "nothing queued")
    return Finding("media", "renders", "warn",
                   f"{open_runs} render(s) not collected",
                   fix="nothing polls them on a timer — POST /api/media/runs/{id}/poll",
                   facts={"open": open_runs})


def _skills() -> Finding:
    """How many skills are stored, and how many a backend could actually run.

    Two numbers on purpose, the same pair the audit route reports: almost
    every skill written before the manifest bridge existed is *valid* and
    *not runnable*, because it declares no permissions and deny-by-default
    means no backend may take it. That is the normal state, not a fault."""
    try:
        from services.memory.skills import SkillsManager   # noqa: PLC0415
        from src.constants import DATA_DIR
        from src.skills_runtime import bridge

        manager = SkillsManager(DATA_DIR)
        stored = [s for s in (manager._read_skill(p)
                              for p in manager._iter_skill_files())
                  if s is not None]
    except Exception as e:
        # NOT "no skills": a lookup that failed and an empty list are different
        # answers, and reporting the first as the second is exactly the
        # rounding this whole report exists to refuse. This one caught a real
        # wrong answer the first time it ran.
        return Finding("skills", "installed", "unknown",
                       f"the skills store could not be read: {type(e).__name__}: {e}",
                       fix="this says nothing about whether skills are installed")

    if not stored:
        return Finding("skills", "installed", "absent", "no skills stored",
                       fix="a skill is how a capability gets a manifest; without "
                           "one the contracts layer has nothing to authorise")
    results = bridge.survey(stored)
    runnable = sum(1 for r in results if getattr(r, "runnable", False))
    valid = sum(1 for r in results if getattr(r, "ok", False))
    return Finding("skills", "installed", "ok" if valid else "warn",
                   f"{len(results)} stored · {valid} with a valid manifest · "
                   f"{runnable} runnable right now",
                   fix="" if runnable else "valid and not runnable is the normal "
                                           "state: a skill that declares no backend "
                                           "runs nowhere, on purpose",
                   facts={"count": len(results), "valid": valid,
                          "runnable": runnable})


# ── the report ────────────────────────────────────────────────────────────

def run(*, areas: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ask everything, and say what is worth doing about it."""
    probes: List[Tuple[str, str, Callable]] = [
        ("runtime", "python", _python),
        ("runtime", "git", _git),
        ("runtime", "data directory", _data_dir),
        ("execution", "sandbox image", _sandbox_image),
        ("execution", "agent shell in the sandbox", _agent_sandbox),
        ("coding", "checkpoints", _checkpoints),
        ("coding", "test runner", _tests_runner),
        ("media", "engines", _media_engines),
        ("media", "templates", _media_templates),
        ("media", "renders", _media_runs),
        ("approvals", "pending cards", _approvals),
        ("workflows", "runs", _workflows),
        ("skills", "installed", _skills),
    ]

    findings: List[Finding] = []
    try:
        findings.extend(_backends())
    except Exception as e:
        findings.append(Finding("backends", "registry", "unknown",
                                f"the check itself failed: {e}"))
    for area, name, probe in probes:
        if areas and area not in areas:
            continue
        findings.append(_check(area, name)(probe)())

    if areas:
        findings = [f for f in findings if f.area in areas]

    worst = min((_RANK[f.state] for f in findings), default=_RANK["ok"])
    counts: Dict[str, int] = {}
    for finding in findings:
        counts[finding.state] = counts.get(finding.state, 0) + 1

    return {
        "ok": worst >= _RANK["absent"],
        "checked_at": now_iso(),
        "worst": STATES[worst],
        "counts": counts,
        "findings": [f.to_dict() for f in findings],
        "note": "`absent` is a fact about this machine, not a fault. Nothing "
                "reports ok that was not actually checked.",
    }


MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL",
        "unknown": "?   ", "absent": "--  "}


def render(report: Dict[str, Any], *, verbose: bool = False) -> str:
    """One screen. Problems first, because that is what the reader came for."""
    lines = [f"Faustus doctor · {report['checked_at']} · worst: {report['worst']}"]
    order = {name: i for i, name in enumerate(STATES)}
    findings = sorted(report["findings"],
                      key=lambda f: (order[f["state"]], f["area"], f["name"]))
    # Grouped by STATE, with the area on the line. Grouping by area instead
    # would print the same header three times — once per state something in
    # that area happens to be in — which is how a short report starts looking
    # like a long one.
    heading = {"fail": "broken", "unknown": "could not be checked",
               "warn": "worth a look", "absent": "not on this machine",
               "ok": "working"}
    state = None
    for finding in findings:
        if not verbose and finding["state"] == "ok":
            continue
        if finding["state"] != state:
            state = finding["state"]
            lines.append(f"  {heading.get(state, state).upper()}")
        lines.append(f"    {MARK.get(finding['state'], '?')} "
                     f"{finding['area']}/{finding['name']}: {finding['detail']}")
        if finding["fix"]:
            lines.append(f"         → {finding['fix']}")
    if not verbose:
        okays = [f"{f['area']}/{f['name']}" for f in findings if f["state"] == "ok"]
        if okays:
            lines.append(f"  working: {', '.join(okays)}")
    counts = " · ".join(f"{v} {k}" for k, v in sorted(report["counts"].items()))
    lines.append(f"  {counts}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m src.doctor [--verbose] [--json] [--area runtime ...]`

    Exit code 1 only for a real `fail`. A machine with no ComfyUI is not a
    broken machine, and a doctor that exits non-zero for every absent
    capability would be useless in a script."""
    args = list(argv if argv is not None else sys.argv[1:])
    verbose = "--verbose" in args or "-v" in args
    as_json = "--json" in args
    areas = [args[i + 1] for i, a in enumerate(args)
             if a == "--area" and i + 1 < len(args)] or None

    report = run(areas=areas)
    if as_json:
        import json
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report, verbose=verbose))
    return 1 if report["worst"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
