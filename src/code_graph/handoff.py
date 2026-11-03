"""src/code_graph/handoff.py -- client handoff package with verified instructions.

Builds install / configure / migrate / run / test / recover / demo instructions
from the repository intake dossier, then executes them in a throwaway copy of
the tree (no .git, no virtualenvs, no node_modules) and records, per step, the
command, exit code, duration, the last output lines and a verdict:

* `verified` -- the command ran in the copy and did what it claims;
* `failed`   -- it ran and did not;
* `not run`  -- it could not be tried, with the reason (no network, tool
                missing, execution disabled, nothing to probe...).

`HANDOFF.md` presents only verified commands as instructions. Failed and
not-run steps are listed in a "Not verified" section, with the reason. The
document is written for the client's project and never refers to the tooling
that produced it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .dossier import _Files, dossier as _dossier
from .query import _is_test_path, _root

logger = logging.getLogger(__name__)

_COPY_SKIP = {".git", ".hg", ".svn", ".venv", "venv", "env", ".env.d", "node_modules", "__pycache__",
              ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", ".handoff", ".next", ".cache"}
_COPY_MAX_FILES = 20000
_COPY_MAX_BYTES = 300 * 1024 * 1024
_TAIL_LINES = 12
_PHASES = ("install", "configure", "migrate", "run", "test", "recover", "demo")
_PLACEHOLDER = "changeme"
_PORT_RES = [re.compile(r"""--port[ =]\s*(\d{4,5})"""), re.compile(r"""\bport\s*=\s*(\d{4,5})\b"""),
             re.compile(r"""\.listen\(\s*(\d{4,5})"""), re.compile(r"""\bPORT\b[^\n]{0,40}?(\d{4,5})""")]
_FORBIDDEN_WORDS = re.compile(r"faustus|code[_ ]graph|odysseus", re.I)


# ── process helpers ──────────────────────────────────────────────────────

def _tail(text: str, n: int = _TAIL_LINES) -> List[str]:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:]


def _venv_bin(venv: str, exe: str) -> str:
    sub = "Scripts" if os.name == "nt" else "bin"
    return os.path.join(venv, sub, exe + (".exe" if os.name == "nt" else ""))


def _run(cmd: Sequence[str] | str, cwd: str, env: Dict[str, str], timeout: float, shell: bool = False
         ) -> Tuple[int, str, float, bool]:
    """(exit code, combined output, seconds, timed_out)."""
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                start_new_session=(os.name != "nt"))
    except OSError as exc:
        return 127, f"cannot start: {exc}", time.time() - t0, False
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", time.time() - t0, False
    except subprocess.TimeoutExpired:
        _kill(proc)
        out = ""
        try:
            out, _ = proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        return -9, out or "", time.time() - t0, True


def _kill(proc: "subprocess.Popen[str]") -> None:
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            # A shell=True child is cmd.exe; terminate() would leave the app it
            # started running and holding the output pipe open.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=15)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _network_ok(url: str = "https://registry.npmjs.org/", timeout: float = 4.0) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True      # the server answered
    except Exception:  # noqa: BLE001
        return False


def _copy_tree(root: str, dest: str) -> Tuple[bool, str]:
    count, size = 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _COPY_SKIP]
        rel_dir = os.path.relpath(dirpath, root)
        os.makedirs(os.path.join(dest, rel_dir) if rel_dir != "." else dest, exist_ok=True)
        for name in filenames:
            src = os.path.join(dirpath, name)
            try:
                st = os.stat(src)
            except OSError:
                continue
            if not os.path.isfile(src) or st.st_size > 50 * 1024 * 1024:
                continue
            count += 1
            size += st.st_size
            if count > _COPY_MAX_FILES or size > _COPY_MAX_BYTES:
                return False, f"the tree is larger than the copy limit ({_COPY_MAX_FILES} files / {_COPY_MAX_BYTES // 1048576} MB)"
            target = os.path.join(dest, rel_dir, name) if rel_dir != "." else os.path.join(dest, name)
            try:
                shutil.copy2(src, target)
            except OSError:
                continue
    return True, f"{count} files"


# ── step model ───────────────────────────────────────────────────────────

def _step(phase: str, title: str, command: Optional[str], evidence: str = "", **extra: Any) -> Dict[str, Any]:
    s = {"id": "", "phase": phase, "title": title, "command": command, "evidence": evidence,
         "exit_code": None, "duration_s": None, "output_tail": [], "verdict": "not run", "reason": ""}
    s.update(extra)
    return s


def _first_line(files: _Files, rel: str, needle: str) -> int:
    for i, ln in enumerate(files.lines(rel), 1):
        if needle in ln:
            return i
    return 1


def _env_vars(files: _Files, d: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for f in d["sections"].get("integrations", []):
        if not str(f.get("key", "")).startswith("env:"):
            continue
        name = f["key"][4:]
        rel, _, ln = str(f.get("evidence", "")).rpartition(":")
        default = None
        try:
            line = files.lines(rel)[int(ln) - 1]
        except (ValueError, IndexError):
            line = ""
        m = re.search(re.escape(name) + r"""['"]?\s*(?:,|\|\||\?\?)\s*(['"][^'"]*['"]|[\w.\-]+)""", line)
        if f.get("has_default") and m:
            default = m.group(1).strip("'\"")
        out.append({"name": name, "required": not f.get("has_default"), "default": default,
                    "evidence": f.get("evidence", "")})
    return out


def _py_reqs(files: _Files) -> Tuple[Optional[str], str]:
    for rel in files.paths:
        if re.fullmatch(r"requirements\.txt", rel):
            return rel, f"pip install -r {rel}"
    reqs = [p for p in files.paths if re.fullmatch(r"requirements/[^/]*\.txt", p)]
    if reqs:
        return reqs[0], f"pip install -r {reqs[0]}"
    for rel in files.paths:
        if rel == "pyproject.toml" or rel == "setup.py":
            return rel, "pip install ."
    return None, ""


def _uses_pytest(files: _Files) -> bool:
    for rel in files.paths:
        if rel.endswith(".py") and _is_test_path(rel):
            txt = files.text(rel)
            if re.search(r"^\s*import pytest|^\s*from pytest|^def test_|^async def test_", txt, re.M):
                return True
    for rel in files.paths:
        if re.search(r"requirements[^/]*\.txt$|pyproject\.toml$|setup\.cfg$|tox\.ini$|pytest\.ini$", rel) \
                and "pytest" in files.text(rel):
            return True
    return False


def _uses_unittest(files: _Files) -> bool:
    return any(rel.endswith(".py") and _is_test_path(rel) and "unittest.TestCase" in files.text(rel)
               for rel in files.paths)


def _find_ports(files: _Files, entry_rel: str) -> Tuple[Optional[int], bool, str]:
    """(literal port, code reads PORT env var, evidence)."""
    reads_env = False
    cand: List[Tuple[int, str]] = []
    order = [entry_rel] + [p for p in files.code_files() if p != entry_rel]
    for rel in order:
        if not rel:
            continue
        for i, line in enumerate(files.lines(rel), 1):
            if re.search(r"""(?:environ|getenv|process\.env)[^\n]*\bPORT\b""", line):
                reads_env = True
            for rx in _PORT_RES:
                m = rx.search(line)
                if m and 1024 <= int(m.group(1)) <= 65535:
                    cand.append((int(m.group(1)), f"{rel}:{i}"))
                    break
        if cand and rel == entry_rel:
            break
    if cand:
        return cand[0][0], reads_env, cand[0][1]
    return None, reads_env, ""


def _make_targets(files: _Files) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for rel in files.paths:
        if rel == "Makefile":
            for i, line in enumerate(files.lines(rel), 1):
                m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
                if m:
                    out.setdefault(m.group(1), i)
    return out


def _script(files: _Files, pattern: str) -> Optional[str]:
    rx = re.compile(pattern, re.I)
    for rel in files.paths:
        base = rel.rsplit("/", 1)[-1]
        if rel.startswith(("scripts/", "bin/", "tools/", "ops/")) or "/" not in rel:
            if rx.match(base) and base.rsplit(".", 1)[-1] in ("sh", "py", "bash", "js", "ps1"):
                return rel
    return None


def _script_cmd(rel: str) -> str:
    ext = rel.rsplit(".", 1)[-1]
    return {"sh": f"bash {rel}", "bash": f"bash {rel}", "py": f"python {rel}", "js": f"node {rel}"}.get(ext, rel)


# ── plan ─────────────────────────────────────────────────────────────────

def _plan(files: _Files, d: Dict[str, Any], free_port: int) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    notes: List[str] = []
    sections = d["sections"]
    pkgs = [p for p in files.paths if p.endswith("package.json") and p.count("/") == 0]
    pkg = json.loads(files.text(pkgs[0])) if pkgs else {}
    scripts = pkg.get("scripts") or {}
    py_req_rel, py_install = _py_reqs(files)
    has_py = bool(py_req_rel) or any(p.endswith(".py") for p in files.paths)
    make = _make_targets(files)
    envs = _env_vars(files, d)

    # install
    if has_py:
        steps.append(_step("install", "Create an isolated Python environment", "python -m venv .venv", "", kind="venv"))
        if py_install:
            steps.append(_step("install", "Install the Python dependencies", py_install.replace("pip ", ".venv/bin/pip ", 1)
                               if False else py_install,
                               f"{py_req_rel}:1", kind="pip", requirement_file=py_req_rel))
    if pkg:
        lock = any(p == "package-lock.json" for p in files.paths)
        steps.append(_step("install", "Install the Node dependencies", "npm ci" if lock else "npm install",
                           f"package.json:1", kind="npm"))

    # configure (a table, verified through the run/test steps)
    example = next((p for p in files.paths if re.fullmatch(r"\.env\.(example|sample|template)|env\.example", p)), None)
    steps.append(_step("configure", "Set the environment variables", f"cp {example} .env" if example else None,
                       f"{example}:1" if example else "", kind="configure", env=envs))

    # migrate
    mig_cmd, mig_ev = None, ""
    if "alembic.ini" in files.paths:
        mig_cmd, mig_ev = "alembic upgrade head", f"alembic.ini:1"
    elif "manage.py" in files.paths and "django" in files.text("manage.py").lower():
        mig_cmd, mig_ev = "python manage.py migrate", "manage.py:1"
    elif any(p.endswith("schema.prisma") for p in files.paths):
        sp = next(p for p in files.paths if p.endswith("schema.prisma"))
        mig_cmd, mig_ev = "npx prisma migrate deploy", f"{sp}:1"
    else:
        nm = next((n for n in scripts if re.search(r"migrat", n)), None)
        if nm:
            mig_cmd, mig_ev = f"npm run {nm}", f"package.json:{_first_line(files, 'package.json', nm)}"
        elif "migrate" in make:
            mig_cmd, mig_ev = "make migrate", f"Makefile:{make['migrate']}"
        else:
            sc = _script(files, r"(migrate|db[_-]?init|init[_-]?db)")
            if sc:
                mig_cmd, mig_ev = _script_cmd(sc), f"{sc}:1"
    if mig_cmd:
        steps.append(_step("migrate", "Apply the database migrations", mig_cmd, mig_ev, kind="migrate"))

    # run
    run_cmd, run_ev, entry_rel = None, "", ""
    mains = [f for f in sections.get("entry_points", []) if str(f.get("key", "")).startswith("main:")]
    web_py = [f for f in sections.get("entry_points", []) if str(f.get("key", "")).startswith("web:") and ".py" in f.get("evidence", "")]
    procfile = next((p for p in files.paths if p == "Procfile"), None)
    if procfile:
        m = next(((i, re.match(r"^web:\s*(.+)$", ln)) for i, ln in enumerate(files.lines(procfile), 1)
                  if re.match(r"^web:\s*(.+)$", ln)), None)
        if m:
            run_cmd, run_ev = m[1].group(1).replace("$PORT", str(free_port)), f"Procfile:{m[0]}"
    if not run_cmd and "start" in scripts and pkg:
        run_cmd, run_ev = "npm start", f"package.json:{_first_line(files, 'package.json', 'start')}"
    if not run_cmd and "manage.py" in files.paths and "django" in files.text("manage.py").lower():
        run_cmd, run_ev, entry_rel = f"python manage.py runserver 127.0.0.1:{free_port}", "manage.py:1", "manage.py"
    if not run_cmd and (mains or web_py):
        names = ["app.py", "main.py", "server.py", "run.py", "wsgi.py"]
        cand = [f["key"].split(":", 1)[1] for f in mains if f["key"].split(":", 1)[1].count("/") == 0]
        cand.sort(key=lambda p: names.index(p) if p in names else 99)
        if cand:
            entry_rel = cand[0]
            run_cmd, run_ev = f"python {entry_rel}", next(f["evidence"] for f in mains if f["key"].endswith(entry_rel))
        elif web_py:
            rel = web_py[0]["evidence"].rsplit(":", 1)[0]
            txt = files.text(rel)
            mod = rel[:-3].replace("/", ".")
            if "FastAPI(" in txt and re.search(r"uvicorn", files.text(py_req_rel) if py_req_rel else "", re.I):
                var = re.search(r"^(\w+)\s*=\s*FastAPI\(", txt, re.M)
                run_cmd = f"uvicorn {mod}:{var.group(1) if var else 'app'} --port {free_port}"
                entry_rel, run_ev = rel, web_py[0]["evidence"]
            elif "Flask(" in txt:
                var = re.search(r"^(\w+)\s*=\s*Flask\(", txt, re.M)
                run_cmd = f"python -m flask --app {mod}:{var.group(1) if var else 'app'} run --port {free_port}"
                entry_rel, run_ev = rel, web_py[0]["evidence"]
    if run_cmd:
        port, reads_env, port_ev = _find_ports(files, entry_rel)
        steps.append(_step("run", "Start the application", run_cmd, run_ev, kind="run", port=port, port_from_env=reads_env,
                           port_evidence=port_ev, free_port=free_port))

    # test
    test_cmd, test_ev = None, ""
    test_script = next((n for n in scripts if re.fullmatch(r"test(:unit)?|tests?", n)), None)
    if test_script:
        test_cmd, test_ev = f"npm run {test_script}" if test_script != "test" else "npm test", \
            f"package.json:{_first_line(files, 'package.json', test_script)}"
    elif "test" in make:
        test_cmd, test_ev = "make test", f"Makefile:{make['test']}"
    elif has_py and _uses_pytest(files):
        test_cmd, test_ev = "python -m pytest -q", next((f["evidence"] for f in sections.get("test", []) if f.get("evidence")), "")
    elif has_py and _uses_unittest(files):
        test_cmd = "python -m unittest discover -s tests" if any(p.startswith("tests/") for p in files.paths) \
            else "python -m unittest discover"
        test_ev = next((f["evidence"] for f in sections.get("test", []) if f.get("evidence")), "")
    if test_cmd:
        steps.append(_step("test", "Run the test suite", test_cmd, test_ev, kind="test"))

    # recover
    data_found = [f for f in sections.get("data", []) if re.match(r"(db|orm|ddl|sql|migrations|prisma)", str(f.get("key", "")))]
    bk = _script(files, r"back-?up")
    rs = _script(files, r"restore")
    if bk or "backup" in make:
        cmd = f"make backup" if "backup" in make and not bk else _script_cmd(bk)
        steps.append(_step("recover", "Back up the data", cmd, f"{bk or 'Makefile'}:{1 if bk else make['backup']}", kind="backup"))
    if rs or "restore" in make:
        cmd = f"make restore" if "restore" in make and not rs else _script_cmd(rs)
        steps.append(_step("recover", "Restore the data", cmd, f"{rs or 'Makefile'}:{1 if rs else make['restore']}",
                           kind="restore"))
    if data_found and not (bk or rs or "backup" in make or "restore" in make):
        notes.append(f"A data store was found ({data_found[0]['fact']}, {data_found[0]['evidence']}) but the repository has "
                     "no backup or restore command. Back up the database or data folder with the tooling of your hosting.")

    # demo
    demo = _script(files, r"(demo|seed|sample)")
    if "demo" in make:
        steps.append(_step("demo", "Load demo data / start the demo", "make demo", f"Makefile:{make['demo']}", kind="demo"))
    elif demo:
        steps.append(_step("demo", "Load demo data / start the demo", _script_cmd(demo), f"{demo}:1", kind="demo"))
    else:
        steps.append(_step("demo", "Open the running application", None, "", kind="demo_open"))

    for i, s in enumerate(steps, 1):
        s["id"] = f"{s['phase']}-{i}"
    return {"steps": steps, "env": envs, "notes": notes, "has_python": has_py, "has_node": bool(pkg)}


# ── execution ────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, work: str, timeout: float, allow_network: bool, envs: List[Dict[str, Any]]):
        self.work, self.timeout, self.allow_network = work, timeout, allow_network
        self.venv = os.path.join(work, ".venv")
        self.venv_ok = False
        self.node_ok = False
        self.network: Optional[bool] = None
        base = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
        base["PYTHONDONTWRITEBYTECODE"] = "1"
        base["PYTHONUNBUFFERED"] = "1"
        self.base_env = base
        self.cfg_env = dict(base)
        for e in envs:
            if e["name"] not in self.cfg_env:
                self.cfg_env[e["name"]] = e["default"] if e["default"] is not None else _PLACEHOLDER
        self.placeholders = [e["name"] for e in envs if e["required"] and e["name"] not in base]

    def env(self) -> Dict[str, str]:
        env = dict(self.cfg_env)
        if self.venv_ok:
            env["VIRTUAL_ENV"] = self.venv
            env["PATH"] = os.path.dirname(_venv_bin(self.venv, "python")) + os.pathsep + env.get("PATH", "")
        return env

    def net(self) -> bool:
        if self.network is None:
            self.network = bool(self.allow_network and _network_ok())
        return self.network


def _finish(step: Dict[str, Any], code: Optional[int], out: str, secs: float, verdict: str, reason: str = "") -> None:
    step.update(exit_code=code, output_tail=_tail(out), duration_s=round(secs, 2), verdict=verdict, reason=reason)


def _not_run(step: Dict[str, Any], reason: str) -> None:
    step.update(verdict="not run", reason=reason)


def _probe(port: int, deadline: float) -> Tuple[Optional[str], str]:
    for path in ("/health", "/healthz", "/api/health", "/"):
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return url, f"HTTP {r.status}"
        except urllib.error.HTTPError as exc:
            if path == "/":
                return url, f"HTTP {exc.code}"
            continue
        except Exception:  # noqa: BLE001
            continue
    return None, ""


def _exec_run(step: Dict[str, Any], ctx: _Ctx, install_failed: bool) -> None:
    cmd = step["command"]
    if install_failed:
        return _not_run(step, "the install step did not succeed, so the application cannot be started here")
    env = ctx.env()
    port = step.get("free_port")
    if step.get("port_from_env") or step.get("port") is None:
        env["PORT"] = str(port)
        probe_port: Optional[int] = port if (step.get("port_from_env") or str(port) in cmd) else None
    else:
        probe_port = step["port"]
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=ctx.work, env=env, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                start_new_session=(os.name != "nt"))
    except OSError as exc:
        return _finish(step, 127, str(exc), 0, "failed", "the command could not be started")
    limit = min(ctx.timeout, 25.0)
    url, how = None, ""
    exited = None
    while time.time() - t0 < limit:
        exited = proc.poll()
        if exited is not None:
            break
        if probe_port:
            url, how = _probe(probe_port, t0 + limit)
            if url:
                break
        time.sleep(0.4)
    secs = time.time() - t0
    out = ""
    if exited is None:
        _kill(proc)
    try:
        out, _ = proc.communicate(timeout=5)
        out = out or ""
    except Exception:  # noqa: BLE001
        pass
    code = proc.returncode
    if url:
        step.update(url=url.rsplit("/", 1)[0] + "/", probe=how)
        return _finish(step, None, out, secs, "verified", f"answered {how} at {url}")
    if exited is not None:
        if exited == 0:
            return _finish(step, 0, out, secs, "verified", "ran to completion with exit code 0 (no server to probe)")
        return _finish(step, exited, out, secs, "failed", f"exited with code {exited} before answering")
    if probe_port:
        return _finish(step, None, out, secs, "failed", f"no HTTP answer on port {probe_port} within {int(limit)} s")
    return _finish(step, None, out, secs, "not run", "the process stayed up but no port was found to probe, so it was not confirmed")


def _execute(plan: Dict[str, Any], work: str, timeout: float, allow_network: bool) -> Dict[str, Any]:
    steps = plan["steps"]
    ctx = _Ctx(work, timeout, allow_network, plan["env"])
    install_failed = False
    py = sys.executable or "python3"
    for s in steps:
        kind, cmd = s["kind"], s["command"]
        if kind == "venv":
            code, out, secs, to = _run([py, "-m", "venv", ctx.venv], work, ctx.base_env, timeout)
            ctx.venv_ok = code == 0 and os.path.exists(_venv_bin(ctx.venv, "python"))
            _finish(s, code, out, secs, "verified" if ctx.venv_ok else "failed", "" if ctx.venv_ok else "venv creation failed")
            install_failed = install_failed or not ctx.venv_ok
        elif kind == "pip":
            if not ctx.venv_ok:
                _not_run(s, "no virtual environment to install into")
                install_failed = True
                continue
            tail = cmd.split(None, 1)[1]
            code, out, secs, to = _run(f"{_venv_bin(ctx.venv, 'python')} -m pip --disable-pip-version-check {tail}",
                                       work, ctx.env(), timeout, shell=True)
            if code == 0:
                _finish(s, code, out, secs, "verified")
            else:
                _finish(s, code, out, secs, "failed", "timed out" if to else
                        ("no network to download packages" if re.search(r"Connection|Temporary failure|resolve|Network", out) else "pip returned an error"))
                install_failed = True
        elif kind == "npm":
            if not shutil.which("npm"):
                _not_run(s, "npm is not installed on the verifying machine")
                continue
            if not ctx.net():
                _not_run(s, "network unreachable (or disabled): npm cannot download packages here")
                continue
            code, out, secs, to = _run(cmd, work, ctx.env(), timeout, shell=True)
            ctx.node_ok = code == 0
            _finish(s, code, out, secs, "verified" if code == 0 else "failed", "timed out" if to else "")
        elif kind == "configure":
            if cmd:
                code, out, secs, to = _run(cmd, work, ctx.env(), 30, shell=True)
                _finish(s, code, out, secs, "verified" if code == 0 else "failed")
            s["placeholders_used"] = ctx.placeholders
        elif kind in ("migrate", "backup", "restore", "demo"):
            first = cmd.split()[0] if cmd else ""
            if first in ("npm", "npx") and not ctx.node_ok:
                _not_run(s, "needs the Node dependencies, which could not be installed here")
                continue
            if first in ("python", "alembic") and not ctx.venv_ok:
                _not_run(s, "needs the Python environment, which could not be created")
                continue
            if first == "make" and not shutil.which("make"):
                _not_run(s, "make is not installed on the verifying machine")
                continue
            if first == "bash" and not shutil.which("bash"):
                _not_run(s, "bash is not installed on the verifying machine")
                continue
            if first == "alembic" and not os.path.exists(_venv_bin(ctx.venv, "alembic")):
                _not_run(s, "alembic is not in the dependencies that were installed")
                continue
            code, out, secs, to = _run(cmd, work, ctx.env(), timeout, shell=True)
            _finish(s, code, out, secs, "verified" if code == 0 else "failed",
                    "timed out" if to else ("" if code == 0 else f"exit code {code}"))
        elif kind == "run":
            _exec_run(s, ctx, install_failed and plan["has_python"] and not s["command"].startswith("npm"))
        elif kind == "test":
            first = cmd.split()[0]
            if first in ("npm",) and not ctx.node_ok:
                _not_run(s, "needs the Node dependencies, which could not be installed here")
                continue
            if cmd.startswith("python -m pytest") and ctx.venv_ok:
                probe_code, _o, _s, _t = _run([_venv_bin(ctx.venv, "python"), "-c", "import pytest"], work, ctx.env(), 30)
                if probe_code != 0:
                    if ctx.net():
                        _run([_venv_bin(ctx.venv, "python"), "-m", "pip", "install", "-q", "pytest"], work, ctx.env(), timeout)
                        s["note"] = "pytest is not in the repository's requirements; it was installed on top"
                    else:
                        _not_run(s, "pytest is not in the repository's requirements and cannot be downloaded without network")
                        continue
            if first in ("python", "pytest") and not ctx.venv_ok:
                _not_run(s, "needs the Python environment, which could not be created")
                continue
            code, out, secs, to = _run(cmd, work, ctx.env(), timeout, shell=True)
            _finish(s, code, out, secs, "verified" if code == 0 else "failed",
                    "timed out" if to else ("" if code == 0 else f"exit code {code}"))
    # configure and demo_open derive from the other steps
    run = next((s for s in steps if s["kind"] == "run"), None)
    for s in steps:
        if s["kind"] == "configure" and s["verdict"] == "not run" and not s["command"]:
            ok = [x for x in steps if x["kind"] in ("run", "test") and x["verdict"] == "verified"]
            if ok:
                s["verdict"] = "verified"
                s["reason"] = (f"the {ok[0]['phase']} step worked with these variables set"
                               + (f"; required ones used the placeholder `{_PLACEHOLDER}`" if ctx.placeholders else ""))
            else:
                s["reason"] = "no run or test step succeeded, so the variables were not exercised"
        if s["kind"] == "demo_open":
            if run and run["verdict"] == "verified" and run.get("url"):
                s.update(verdict="verified", reason=f"the application answered at {run['url']}", url=run["url"])
            else:
                s["reason"] = "the application could not be started and probed"
    return {"venv": ctx.venv_ok, "node": ctx.node_ok, "network": ctx.network, "placeholders": ctx.placeholders}


# ── HANDOFF.md ───────────────────────────────────────────────────────────

def _fence(cmds: Sequence[str]) -> str:
    return "```bash\n" + "\n".join(cmds) + "\n```\n"


def _render_md(name: str, plan: Dict[str, Any], run_info: Dict[str, Any], ran: bool, root_label: str) -> str:
    steps = plan["steps"]
    ok = [s for s in steps if s["verdict"] == "verified"]
    bad = [s for s in steps if s["verdict"] != "verified"]
    L: List[str] = [f"# {name} - handoff", ""]
    L.append("This document lists the commands that were run, from a clean copy of the repository, "
             "with the result of each. Commands under \"Not verified\" are not instructions: they "
             "failed or could not be tried, and the reason is given." if ran else
             "No command was executed to prepare this document. Nothing in it is verified.")
    L.append("")
    L.append(f"Verified on {sys.platform}, Python {sys.version_info.major}.{sys.version_info.minor}"
             + (", with network access" if run_info.get("network") else ", without network access") + ".")
    L.append("")
    titles = {"install": "Install", "configure": "Configure", "migrate": "Migrate the database", "run": "Run",
              "test": "Test", "recover": "Backup and recovery", "demo": "Demo"}
    for phase in _PHASES:
        verified = [s for s in ok if s["phase"] == phase]
        if not verified:
            continue
        L.append(f"## {titles[phase]}")
        L.append("")
        if phase == "configure":
            env = plan["env"]
            if env:
                L.append("| Variable | Required | Default | Where it is read |")
                L.append("|---|---|---|---|")
                for e in env:
                    L.append(f"| `{e['name']}` | {'yes' if e['required'] else 'no'} | "
                             f"{('`' + e['default'] + '`') if e['default'] not in (None, '') else '-'} | {e['evidence']} |")
                L.append("")
                if run_info.get("placeholders"):
                    L.append(f"Required variables were set to `{_PLACEHOLDER}` for verification. Replace them with real values.")
                    L.append("")
            cmds = [s["command"] for s in verified if s["command"]]
            if cmds:
                L.append(_fence(cmds))
            continue
        for s in verified:
            L.append(f"**{s['title']}**" + (f" (took {s['duration_s']} s)" if s.get("duration_s") else ""))
            L.append("")
            if s["command"]:
                L.append(_fence([s["command"]]))
            if s["verdict"] == "verified" and s.get("reason"):
                L.append(f"Result: {s['reason']}.")
                L.append("")
            if s.get("note"):
                L.append(f"Note: {s['note']}.")
                L.append("")
    if bad or plan["notes"]:
        L.append("## Not verified")
        L.append("")
        for s in bad:
            L.append(f"- **{s['title']}** ({s['verdict']})" + (f": `{s['command']}`" if s["command"] else ""))
            if s["reason"]:
                L.append(f"  - Reason: {s['reason']}")
            if s.get("exit_code") not in (None, 0):
                L.append(f"  - Exit code: {s['exit_code']}")
            for ln in s.get("output_tail", [])[-6:]:
                L.append(f"  - `{ln[:160]}`")
        for n in plan["notes"]:
            L.append(f"- {n}")
        L.append("")
    return _scrub("\n".join(L).rstrip() + "\n")


def _scrub(text: str) -> str:
    """The client document never names the tooling that produced it."""
    return "\n".join(_FORBIDDEN_WORDS.sub("the tool", ln) if _FORBIDDEN_WORDS.search(ln) else ln
                     for ln in text.split("\n"))


# ── entry point ──────────────────────────────────────────────────────────

def handoff(root: str = "", out_dir: Optional[str] = None, run: bool = True, timeout: float = 120,
            *, project_id: str = "", allow_network: bool = True, output_chars: int = 6000) -> Dict[str, Any]:
    """Build and verify client handoff instructions for the repository at `root`.
    Steps run in a throwaway copy; `HANDOFF.md` and `handoff-report.json` are
    written to `out_dir` (default `<root>/.handoff`)."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        timeout = max(5.0, min(float(timeout or 120), 900.0))
    except (TypeError, ValueError):
        timeout = 120.0
    dos = _dossier(resolved, project_id=project_id)
    if dos.get("exit_code") != 0 or not dos.get("dossier"):
        return {"error": f"handoff: the dossier could not be built: {dos.get('error', 'unknown')}", "exit_code": 1}
    files = _Files(resolved)
    free_port = _free_port()
    plan = _plan(files, dos["dossier"], free_port)
    name = os.path.basename(resolved.rstrip("/\\")) or "project"
    run_info: Dict[str, Any] = {"network": None, "venv": False, "node": False, "placeholders": []}
    copy_note = ""
    if run:
        tmp = tempfile.mkdtemp(prefix="handoff-")
        try:
            work = os.path.join(tmp, "copy")
            ok, copy_note = _copy_tree(resolved, work)
            if not ok:
                for s in plan["steps"]:
                    _not_run(s, copy_note)
            else:
                run_info = _execute(plan, work, timeout, allow_network)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        for s in plan["steps"]:
            _not_run(s, "execution disabled (run=False)")
        copy_note = "not executed"
    md = _render_md(name, plan, run_info, run, resolved)
    target = out_dir or os.path.join(resolved, ".handoff")
    if not os.path.isabs(target):
        target = os.path.join(resolved, target)
    os.makedirs(target, exist_ok=True)
    md_path = os.path.join(target, "HANDOFF.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    counts: Dict[str, int] = {"verified": 0, "failed": 0, "not run": 0}
    for s in plan["steps"]:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
    report = {"root": resolved, "revision": dos.get("revision"), "executed": bool(run), "copy": copy_note,
              "environment": run_info, "steps": plan["steps"], "env": plan["env"], "notes": plan["notes"],
              "counts": counts, "handoff_md": md_path}
    with open(os.path.join(target, "handoff-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
    lines = [f"Handoff for {name}: {counts['verified']} verified, {counts['failed']} failed, {counts['not run']} not run.",
             f"HANDOFF.md: {md_path}"]
    for s in plan["steps"]:
        lines.append(f"  [{s['verdict']}] {s['phase']}: {s['title']}" + (f"  `{s['command']}`" if s["command"] else "")
                     + (f"  - {s['reason']}" if s["reason"] else ""))
    from .query import _clip
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved, "path": md_path,
            "report_path": os.path.join(target, "handoff-report.json"), "counts": counts, "steps": plan["steps"],
            "env": plan["env"], "notes": plan["notes"], "environment": run_info, "markdown": md}
