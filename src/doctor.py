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
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
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
                   fix=(f"{DATA_DIR} is not writable by this user" if not writable
                        else "free disk space before starting another render or checkpoint"
                        if state == "warn" else ""),
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
        "workflows", "runs", "warn" if paused else "ok",
        f"{paused} paused, {running} still going",
        fix=("open Activity to inspect the waiting step: the running server "
             "continues active workflows and elapsed timers automatically, "
             "but a human approval still needs your answer" if paused else ""),
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
    return Finding("media", "renders", "ok",
                   f"{open_runs} render(s) in flight; the running server collects "
                   "submitted jobs automatically — inspect progress in Activity",
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

def _probe_json(url: str) -> Any:
    """Read-only, bounded health request; never follow an endpoint redirect."""
    import httpx

    deadline = time.monotonic() + 6
    body = bytearray()
    with httpx.stream("GET", url, timeout=2.5, follow_redirects=False) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(chunk_size=16384):
            if time.monotonic() > deadline or len(body) + len(chunk) > 1024 * 1024:
                raise ValueError("health response exceeded its budget")
            body.extend(chunk)
    return json.loads(body)


def _models() -> Finding:
    from routes.system_usage_routes import _ollama_base

    try:
        data = _probe_json(_ollama_base() + "/api/tags")
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("name"), str)
                or not m["name"].strip() for m in models):
            raise ValueError("invalid model catalogue")
    except Exception as exc:
        return Finding("models", "Ollama catalogue", "unknown",
                       f"could not verify the local catalogue ({type(exc).__name__})",
                       fix="start Ollama or check OLLAMA_BASE_URL / OLLAMA_HOST; "
                           "cloud API models do not require Ollama")
    return Finding("models", "Ollama catalogue", "ok" if models else "absent",
                   f"{len(models)} installed model(s); no inference was started",
                   fix="" if models else "install a model from Settings → Local models, "
                       "or connect a cloud provider",
                   facts={"count": len(models), "models": [m["name"][:300] for m in models[:100]]})


def _memory_store() -> Finding:
    from src.constants import DATA_DIR

    path = os.path.join(DATA_DIR, "memory.json")
    try:
        with open(path, "rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            return Finding("memory", "stored memories", "unknown",
                           "store exceeds the diagnostic read budget",
                           fix="inspect the memory store with a backup; the diagnostic "
                               "does not truncate or rewrite it")
        records = json.loads(raw)
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError("expected a list of memory records")
    except FileNotFoundError:
        return Finding("memory", "stored memories", "absent", "no memory file yet",
                       fix="save a memory in a chat; this check creates no files")
    except (ValueError, OSError) as exc:
        return Finding("memory", "stored memories", "fail",
                       f"memory store is unreadable ({type(exc).__name__})",
                       fix="back up memory.json and restore a readable copy; "
                           "do not replace it with an empty store")
    return Finding("memory", "stored memories", "ok",
                   f"{len(records)} readable record(s); contents are not included",
                   facts={"count": len(records)})


def _memory_vectors() -> Finding:
    import httpx

    host = os.getenv("CHROMADB_HOST", "localhost")
    port = int(os.getenv("CHROMADB_PORT", "8100"))
    url = str(httpx.URL(scheme="http", host=host, port=port, path="/api/v2/heartbeat"))
    try:
        data = _probe_json(url)
        value = data.get("nanosecond heartbeat") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("invalid heartbeat")
    except Exception as exc:
        return Finding("memory", "Chroma vector service", "unknown",
                       f"heartbeat not verified ({type(exc).__name__}); "
                       "this does not imply stored memories are lost",
                       fix="start ChromaDB or check CHROMADB_HOST / CHROMADB_PORT; "
                           "vector retrieval needs the service, plain memory storage does not")
    return Finding("memory", "Chroma vector service", "ok",
                   "service heartbeat answered; no collection or embedding was created")


# ── first run: the question a brand-new install actually has (SET-01) ──────

def _setup_admin() -> Finding:
    """Read `auth.json` directly, the same file `core.auth.AuthManager` owns —
    no AuthManager instantiated here, so this never takes its locks or starts
    a session store just to answer "does anyone own this install yet"."""
    from src.constants import AUTH_FILE

    if not os.path.isfile(AUTH_FILE):
        return Finding("setup", "admin account", "absent",
                       "no auth.json yet — first run has not created an owner",
                       fix="open the app once and complete first-run setup, or run setup.py")
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, dict):
            raise ValueError("auth.json has no readable users map")
    except Exception as e:
        return Finding("setup", "admin account", "unknown",
                       f"auth.json could not be read ({type(e).__name__})",
                       fix="check auth.json permissions/JSON validity; do not edit it by hand")
    if not users:
        return Finding("setup", "admin account", "absent", "no users created yet",
                       fix="complete first-run setup to create the owner account")
    admins = [u for u, d in users.items() if isinstance(d, dict) and d.get("is_admin")]
    if not admins:
        return Finding("setup", "admin account", "fail",
                       f"{len(users)} user(s), none is_admin",
                       fix="promote an account to admin — some privileged screens "
                           "(Diagnostics, backups) have no other way in",
                       facts={"users": len(users)})
    return Finding("setup", "admin account", "ok", f"{len(admins)} admin account(s)",
                   facts={"admins": len(admins), "users": len(users)})


def _setup_provider() -> Finding:
    """Any usable source of models: a configured endpoint row, OR a local
    Ollama that actually has something installed. Neither on its own proves
    the other absent — a machine can be Ollama-only, endpoint-only, or both."""
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            endpoints = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).count()  # noqa: E712
        finally:
            db.close()
    except Exception as e:
        return Finding("setup", "model provider", "unknown",
                       f"could not read model_endpoints ({type(e).__name__})")

    ollama_has_models = False
    try:
        from routes.system_usage_routes import _ollama_base
        data = _probe_json(_ollama_base() + "/api/tags")
        models = data.get("models") if isinstance(data, dict) else None
        ollama_has_models = isinstance(models, list) and len(models) > 0
    except Exception:
        pass

    if endpoints or ollama_has_models:
        return Finding("setup", "model provider", "ok",
                       f"{endpoints} configured endpoint(s)"
                       + (", local Ollama has models" if ollama_has_models else ""),
                       facts={"endpoints": endpoints, "ollama_has_models": ollama_has_models})
    return Finding("setup", "model provider", "absent",
                   "no model endpoint configured and no local Ollama model installed",
                   fix="add a provider in Settings → Models, or install a model with "
                       "Ollama and Faustus will pick it up",
                   facts={"endpoints": 0, "ollama_has_models": False})


# ── the environment itself, reproducibly (BASE-03 / OPS-01) ────────────────

def _environment_lockfiles() -> Finding:
    """The two lockfiles a fresh clone needs pinned, and whether the
    directories they describe actually exist — a present lockfile with no
    matching install is exactly the "works on my machine" gap OPS-01 names."""
    from src.runtime_paths import get_app_root

    root = Path(get_app_root())
    py_lock = root / "requirements.txt"
    npm_lock = root / "package-lock.json"
    node_modules = root / "node_modules"
    missing = [str(p.name) for p in (py_lock, npm_lock) if not p.is_file()]
    if missing:
        return Finding("environment", "lockfiles", "fail",
                       f"missing: {', '.join(missing)}",
                       fix="a clone without these cannot reproduce this install's "
                           "dependency versions; restore them from version control")
    if not node_modules.is_dir():
        return Finding("environment", "lockfiles", "warn",
                       "package-lock.json is present but node_modules is not installed",
                       fix="npm ci", facts={"repair": "npm_ci"})
    return Finding("environment", "lockfiles", "ok",
                   "requirements.txt, package-lock.json present; node_modules installed")


def _environment_node() -> Finding:
    import subprocess

    node = shutil.which("node")
    if not node:
        return Finding("environment", "node", "fail", "no node on PATH",
                       fix="install Node.js — the Studio build needs it")
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
        version = out.stdout.strip() or out.stderr.strip()
    except Exception as e:
        return Finding("environment", "node", "unknown",
                       f"node is on PATH but did not answer --version ({type(e).__name__})")
    return Finding("environment", "node", "ok", version, facts={"path": node, "version": version})


def _environment_studio_build() -> Finding:
    """Whether Studio's static assets were actually built, not just that
    `npm ci` ran. `npx tsc` is deliberately never invoked from here (COMUN.md:
    it resolves to whatever tsc happens to be on PATH, not this repo's own) —
    this only looks for the build OUTPUT, it never triggers or checks the
    type-check step."""
    from src.runtime_paths import get_app_root

    root = Path(get_app_root())
    dist = root / "static" / "studio"
    if not dist.is_dir() or not any(dist.glob("*.html")) and not any(dist.rglob("*.js")):
        return Finding("environment", "studio assets", "warn",
                       f"no built assets found under {dist}",
                       fix="npx vite build (from the repo root)")
    return Finding("environment", "studio assets", "ok", f"built assets present in {dist}")


def _environment_launcher() -> Finding:
    """launcher.py exists and at least parses — the Windows portable entry
    point failing silently at import time is a machine that looks broken with
    no error a user would ever see (it runs inside a windowed PyInstaller
    bundle with stdout suppressed)."""
    import ast
    from src.runtime_paths import get_app_root

    path = Path(get_app_root()) / "launcher.py"
    if not path.is_file():
        return Finding("environment", "launcher", "absent", "no launcher.py",
                       fix="only needed for the Windows portable build")
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return Finding("environment", "launcher", "fail", f"launcher.py does not parse: {e}",
                       fix="fix the syntax error before building the portable launcher")
    return Finding("environment", "launcher", "ok", str(path))


def _environment_psutil() -> Finding:
    try:
        import psutil
        return Finding("environment", "psutil", "ok", psutil.__version__)
    except Exception:
        return Finding("environment", "psutil", "fail", "psutil is not importable",
                       fix="pip install -r requirements.txt — psutil backs process/"
                           "resource checks used across Diagnostics")


def _environment_ffmpeg() -> Finding:
    path = shutil.which("ffmpeg")
    if not path:
        return Finding("environment", "ffmpeg", "absent", "no ffmpeg on PATH",
                       fix="install ffmpeg if you need audio/video transcoding; "
                           "everything else works without it")
    return Finding("environment", "ffmpeg", "ok", path, facts={"path": path})


def _browser() -> Finding:
    from src.tool_utils import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        return Finding("browser", "integrated browser", "unknown",
                       "no browser manager in this process",
                       fix="run this diagnostic in the running app to inspect its browser")
    status = manager.get_server_status("builtin_browser").get("status", "unknown")
    connected = status == "connected"
    return Finding("browser", "integrated browser", "ok" if connected else "warn",
                   "browser process connected; no page was opened" if connected else
                   f"browser connection is {status}",
                   fix="" if connected else "open Settings → MCP and restart the built-in "
                       "browser; check its installation if it cannot start",
                   facts={"status": status})

def _windows_shell() -> Finding:
    """EXEC-01: on Windows, the agent's bash/python tools need Git Bash.

    Without it every bash call fails one at a time, mid-turn, with a
    `RuntimeError` (src/agent_tools/subprocess_tools.py::_create_bash_subprocess)
    — correct, but a person only learns it the first time an agent tries to
    run a command. This is the same gap asked before it bites, not after.
    """
    from core.platform_compat import IS_WINDOWS, find_bash

    if not IS_WINDOWS:
        return Finding("execution", "windows shell (git bash)", "ok",
                       "not on Windows — the host shell runs commands directly")
    bash = find_bash()
    if not bash:
        return Finding("execution", "windows shell (git bash)", "fail",
                       "no Git Bash found — every bash/python tool call will fail "
                       "the moment an agent tries to run one",
                       fix="install Git for Windows and restart Faustus")
    return Finding("execution", "windows shell (git bash)", "ok", bash,
                   facts={"path": bash})


def _showcase_demo() -> Finding:
    """CMP-14: does this checkout actually have the showcase materials
    `docs/showcase.md` points a reader at, not just the doc naming them.

    Deliberately narrow: this checks the demo MATERIALS are on disk, never
    that the three walkthroughs themselves pass — that is what
    `tests/test_adp08_desktop_semantics.py`/`test_cmp10_channel.py`/
    `test_cmp06_herdr_adapter.py`/etc. are for. Conflating "the file exists"
    with "the walkthrough works" is exactly the kind of rounded-up `ok`
    this module's own docstring warns against.
    """
    from src.runtime_paths import get_app_root

    root = Path(get_app_root())
    showcase_doc = root / "docs" / "showcase.md"
    examples_dir = root / "examples" / "showcase"
    sample_project = examples_dir / "sample-project"

    missing = []
    if not showcase_doc.is_file():
        missing.append(str(showcase_doc.relative_to(root)))
    if not sample_project.is_dir():
        missing.append(str(sample_project.relative_to(root)))
    elif not any(sample_project.iterdir()):
        missing.append(f"{sample_project.relative_to(root)} (empty)")

    if missing:
        return Finding("docs", "showcase demo", "fail",
                       f"missing: {', '.join(missing)}",
                       fix="restore docs/showcase.md and examples/showcase/sample-project/ "
                           "(see docs/showcase.md for what belongs there)")
    return Finding("docs", "showcase demo", "ok",
                   "docs/showcase.md and examples/showcase/sample-project/ are both present",
                   facts={"doc": str(showcase_doc.relative_to(root)),
                          "sample_project": str(sample_project.relative_to(root))})


def run(*, areas: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ask everything, and say what is worth doing about it."""
    probes: List[Tuple[str, str, Callable]] = [
        ("runtime", "python", _python),
        ("runtime", "git", _git),
        ("runtime", "data directory", _data_dir),
        ("setup", "admin account", _setup_admin),
        ("setup", "model provider", _setup_provider),
        ("environment", "lockfiles", _environment_lockfiles),
        ("environment", "node", _environment_node),
        ("environment", "studio assets", _environment_studio_build),
        ("environment", "launcher", _environment_launcher),
        ("environment", "psutil", _environment_psutil),
        ("environment", "ffmpeg", _environment_ffmpeg),
        ("execution", "sandbox image", _sandbox_image),
        ("execution", "agent shell in the sandbox", _agent_sandbox),
        ("execution", "windows shell (git bash)", _windows_shell),
        ("coding", "checkpoints", _checkpoints),
        ("coding", "test runner", _tests_runner),
        ("media", "engines", _media_engines),
        ("media", "templates", _media_templates),
        ("media", "renders", _media_runs),
        ("approvals", "pending cards", _approvals),
        ("workflows", "runs", _workflows),
        ("skills", "installed", _skills),
        ("models", "Ollama catalogue", _models),
        ("memory", "stored memories", _memory_store),
        ("memory", "Chroma vector service", _memory_vectors),
        ("browser", "integrated browser", _browser),
        ("docs", "showcase demo", _showcase_demo),
    ]

    findings: List[Finding] = []
    if not areas or "backends" in areas:
        try:
            findings.extend(_backends())
        except Exception as e:
            findings.append(Finding("backends", "registry", "unknown",
                                    f"the check itself failed: {e}",
                                    fix="inspect the backend registry and service logs"))
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


# ── repair — a short, explicit allowlist, not "run whatever the check said" ─
#
# A Finding's `fix` text is for a person to read. `facts["repair"]` is the
# separate, narrower promise that THIS exact command is safe to run from a
# button: idempotent, scoped to this repo, and not a network install of
# anything beyond what the lockfile already pins. Every other `fix` stays
# text-only on purpose — "pip install -r requirements.txt" runs arbitrary
# packages' setup code, which is not a click a diagnostics panel should offer.

def _studio_root() -> Path:
    from src.runtime_paths import get_app_root
    return Path(get_app_root())


REPAIRS: Dict[str, Dict[str, Any]] = {
    "npm_ci": {
        "description": "npm ci — installs Studio's pinned dependencies from package-lock.json",
        "cwd": _studio_root,
        "argv": ["npm", "ci"],
    },
}


def repair(name: str, *, timeout: float = 300.0) -> Dict[str, Any]:
    """Run one allowlisted repair. Raises ValueError for anything not in
    `REPAIRS` — this is deliberately not a general command runner."""
    import subprocess

    spec = REPAIRS.get(name)
    if spec is None:
        raise ValueError(f"no such repair {name!r}; available: {sorted(REPAIRS)}")
    cwd = spec["cwd"]()
    npm = shutil.which("npm") or (shutil.which("npm.cmd") if os.name == "nt" else None)
    argv = spec["argv"]
    if argv and argv[0] == "npm" and npm:
        argv = [npm, *argv[1:]]
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
        ok = proc.returncode == 0
        return {"ok": ok, "repair": name, "returncode": proc.returncode,
                "seconds": round(time.time() - started, 1),
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "repair": name, "error": f"timed out after {timeout}s"}
    except FileNotFoundError as e:
        return {"ok": False, "repair": name, "error": f"could not run it: {e}"}


# ── first run: the next correct action, never a dead end (SET-01) ──────────

_NEXT_ACTION_ORDER = (
    # (finding area, finding name) -> what to do about it, checked in this
    # order because doing them out of order sends someone in circles (no
    # point picking a model before there is a provider to pick one FROM).
    ("runtime", "data directory", {"label": "Fix the data directory", "route": "/settings"}),
    ("setup", "admin account", {"label": "Finish first-run setup", "route": "/setup"}),
    ("setup", "model provider", {"label": "Connect a model provider", "route": "/settings/models"}),
    ("models", "Ollama catalogue", {"label": "Install or connect a model", "route": "/settings/models"}),
)


def next_setup_action() -> Dict[str, Any]:
    """One recommendation, never a screen with nothing useful to press.

    Walks `_NEXT_ACTION_ORDER` and returns the first gap it finds; `None`
    action means the report found nothing blocking — the caller's empty
    state is then "everything needed is here", not "click this button that
    leads nowhere", which is the dead end SET-01 exists to remove.
    """
    report = run(areas=["runtime", "setup", "models"])
    by_key = {(f["area"], f["name"]): f for f in report["findings"]}
    for area, name, action in _NEXT_ACTION_ORDER:
        finding = by_key.get((area, name))
        if finding and finding["state"] in ("fail", "absent"):
            return {"blocked": True, "finding": finding, "action": action}
    return {"blocked": False, "finding": None, "action": None}


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
