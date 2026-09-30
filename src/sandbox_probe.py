"""Sandbox probe (H02): what each kind of operation really confines.

"The binary exists" and "the daemon answers" are not evidence of confinement.
The probe builds a fixture tree of its own, then tries accesses that must work
and accesses that must fail, through the same routes the agent uses, and
reports the observed result per operation:

* ``shell`` and ``python``: the container backend that ``bash``/``python`` use
  (``execution_backends.DockerWorkspaceBackend`` with the spec the router
  builds), one container per operation running every check;
* ``code_mode``: the confined Code Mode runtime (``src/code_mode/confined.py``);
* ``files``: the file tools, which run on the host under the path policy of
  ``tool_execution._resolve_tool_path_in_roots`` -- reported as host path
  policy, never as a container;
* ``descendants``: that a process started by the guest dies with its container.

Each check states what was expected (``allowed`` / ``denied``), what was
observed, and the evidence line. A check whose probe itself could not run (no
Python in the image, link creation not permitted) is ``inconclusive``, never
silently passed. A backend that cannot be reached makes the operation
``unavailable`` with the reason, never ``verified``.

The fixture lives in a temporary directory that is removed afterwards; nothing
in the user's workspace is read or written. The local HTTP server used for the
network checks binds to an ephemeral port and serves one fixed marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

OPERATIONS = ("shell", "python", "code_mode", "files", "descendants")

#: Documented coverage per operation and host kind. This is a statement about
#: the design, verified (where a container exists) by the checks below.
COVERAGE: Dict[str, Dict[str, str]] = {
    "shell": {
        "posix": "bash runs in the Linux container only when sandbox execution is on and the mode "
                 "is strict or required; in auto mode it falls back to the host and says so.",
        "windows": "bash runs in the Linux container (Docker Desktop) only in required mode, "
                   "where the workspace path is mounted with --mount. In auto and strict mode a "
                   "native Windows host runs it directly. powershell has no container backend: "
                   "it runs on the host, and required mode refuses it.",
    },
    "python": {
        "posix": "same route and policy as bash.",
        "windows": "same as bash: container only in required mode, host otherwise.",
    },
    "code_mode": {
        "posix": "confined runtime (default): container with only the workspace mounted, no "
                 "network unless granted. host runtime is explicit and unconfined.",
        "windows": "confined runtime through Docker Desktop (default). The host runtime uses a "
                   "Job Object for descendants but confines neither files nor network.",
    },
    "files": {
        "posix": "host path policy: the requested path is resolved (symlinks included) and must "
                 "fall inside a declared root. Not an operating-system sandbox.",
        "windows": "host path policy, resolved through realpath so junctions and symlinks are "
                   "followed before the root check. Not an operating-system sandbox.",
    },
    "descendants": {
        "posix": "processes of a confined guest die with its container; host-mode descendants "
                 "are killed as a process tree, best effort.",
        "windows": "confined guests die with their container; host-mode Code Mode descendants are "
                   "contained by a Job Object; host shell descendants are killed as a tree, best "
                   "effort.",
    },
}


def _host_kind() -> str:
    try:
        from core.platform_compat import IS_WINDOWS
        return "windows" if IS_WINDOWS else "posix"
    except Exception:  # pragma: no cover
        return "posix"


def coverage_for_host() -> Dict[str, str]:
    kind = _host_kind()
    return {op: COVERAGE[op][kind] for op in OPERATIONS}


# ── fixture ────────────────────────────────────────────────────────────────

class _Marker(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # silence
        pass


class Fixture:
    """A throw-away tree: ``root`` (the granted workspace) holds an allowed
    file and two links pointing outside; ``sibling`` and ``outside`` are next
    to it and must not be reachable from it."""

    def __init__(self) -> None:
        token = secrets.token_hex(6)
        self.tokens = {k: f"PROBE-{k.upper()}-{token}" for k in
                       ("in", "out", "sib", "net", "env", "write")}
        self.base = tempfile.mkdtemp(prefix="faustus_sbx_probe_")
        # mkdtemp is 0700. Every fixture entry is world-readable on purpose: the
        # only thing that may stop the container reading a forbidden file is the
        # confinement, never a permission bit that would hide a mount hole.
        _open(self.base)
        self.root = os.path.join(self.base, "root")
        self.sibling = os.path.join(self.base, "root2")          # shares the root's prefix
        self.outside = os.path.join(self.base, "outside")
        for path in (self.root, self.sibling, self.outside):
            os.makedirs(path)
            _open(path)
        # The container user is not this user; the granted root is writable to it.
        try:
            os.chmod(self.root, 0o777)
        except OSError:
            pass
        self.allowed = self._file(self.root, "allowed.txt", self.tokens["in"])
        self.decoy = self._file(self.outside, "decoy.txt", self.tokens["out"])
        self.secret = self._file(self.sibling, "secret.txt", self.tokens["sib"])
        self.link_abs = os.path.join(self.root, "link_abs")
        self.link_rel = os.path.join(self.root, "link_rel")
        self.link_dir = os.path.join(self.root, "link_dir")
        self.links_created = all([
            _make_link(self.decoy, self.link_abs),
            _make_link(os.path.join("..", "outside", "decoy.txt"), self.link_rel),
            _make_link(self.outside, self.link_dir, directory=True),
        ])
        self.env_name = f"FAUSTUS_PROBE_ENV_{token.upper()}"
        os.environ[self.env_name] = self.tokens["env"]
        self._server: Optional[HTTPServer] = None
        self.port = 0
        _Marker.body = self.tokens["net"].encode("ascii")
        try:
            self._server = HTTPServer(("0.0.0.0", 0), _Marker)
            self.port = self._server.server_address[1]
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
        except OSError as exc:
            logger.debug("probe http server unavailable: %s", exc)

    @staticmethod
    def _file(directory: str, name: str, text: str) -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return path

    def host_address_from_container(self) -> str:
        if sys.platform.startswith("win") or sys.platform == "darwin":
            return "host.docker.internal"
        try:
            done = subprocess.run(
                ["docker", "network", "inspect", "bridge", "-f",
                 "{{(index .IPAM.Config 0).Gateway}}"], capture_output=True, text=True, timeout=20)
            return done.stdout.strip() or "172.17.0.1"
        except Exception:
            return "172.17.0.1"

    def close(self) -> None:
        os.environ.pop(self.env_name, None)
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        shutil.rmtree(self.base, ignore_errors=True)


def _open(path: str) -> None:
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


def _make_link(target: str, link: str, *, directory: bool = False) -> bool:
    """A symlink, or on Windows (where symlinks need a privilege) a directory
    junction. False when neither could be created, so the checks that depend
    on it report ``inconclusive`` instead of passing vacuously."""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt" and directory:
        try:
            done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                                  capture_output=True, timeout=20)
            return done.returncode == 0
        except Exception:
            return False
    return False


# ── checks ─────────────────────────────────────────────────────────────────

def _expected_inside_read(access: str) -> str:
    return "denied" if access == "none" else "allowed"


def _check_defs(fx: Fixture, *, workspace: str, network: bool, workspace_writable: bool,
                inside_readable: bool, host: str) -> List[Dict[str, Any]]:
    """The checks as data: ``sh`` is the shell command, ``py`` the Python
    expression (evaluating to text) doing the same thing. ``marker`` is the
    text whose presence in the output means the access succeeded."""
    t = fx.tokens
    url = f"http://{host}:{fx.port}/"
    fetch_py = f"urllib.request.urlopen({url!r}, timeout=5).read().decode()"
    fetch_sh = ("python3 -c \"import urllib.request;"
                f"print(urllib.request.urlopen('{url}', timeout=5).read().decode())\"")
    nested = lambda cmd: "sh -c " + shlex.quote(cmd)  # noqa: E731
    missing = ("no such file", "not found", "permission denied", "filenotfounderror",
               "permissionerror", "read-only", "cannot open", "not a directory")
    neterr = ("urlerror", "unreachable", "timed out", "refused", "name or service",
              "resolution", "connection", "network")
    cat = lambda p: f"cat {shlex.quote(p)}"  # noqa: E731
    ws = workspace
    defs = [
        dict(name="read_inside", expected="allowed" if inside_readable else "denied",
             marker=t["in"], hints=missing, sh=cat(f"{ws}/allowed.txt"),
             py=f"open({ws + '/allowed.txt'!r}).read()"),
        dict(name="write_inside", expected="allowed" if workspace_writable else "denied",
             marker=t["write"], hints=missing,
             sh=f"echo {t['write']} > {ws}/probe_written.txt && cat {ws}/probe_written.txt",
             py=(f"(open({ws + '/probe_written.txt'!r}, 'w').write({t['write']!r}), "
                 f"open({ws + '/probe_written.txt'!r}).read())[1]")),
        dict(name="read_outside_absolute_host_path", expected="denied", marker=t["out"], hints=missing,
             sh=cat(fx.decoy), py=f"open({fx.decoy!r}).read()"),
        dict(name="read_outside_relative", expected="denied", marker=t["out"], hints=missing,
             sh=cat(f"{ws}/../outside/decoy.txt"), py=f"open({ws + '/../outside/decoy.txt'!r}).read()"),
        dict(name="read_sibling_absolute_host_path", expected="denied", marker=t["sib"], hints=missing,
             sh=cat(fx.secret), py=f"open({fx.secret!r}).read()"),
        dict(name="read_sibling_relative", expected="denied", marker=t["sib"], hints=missing,
             sh=cat(f"{ws}/../root2/secret.txt"), py=f"open({ws + '/../root2/secret.txt'!r}).read()"),
        dict(name="symlink_absolute_does_not_extend_access", expected="denied", marker=t["out"],
             hints=missing, sh=cat(f"{ws}/link_abs"), py=f"open({ws + '/link_abs'!r}).read()"),
        dict(name="symlink_relative_does_not_extend_access", expected="denied", marker=t["out"],
             hints=missing, sh=cat(f"{ws}/link_rel"), py=f"open({ws + '/link_rel'!r}).read()"),
        dict(name="directory_link_does_not_extend_access", expected="denied", marker=t["out"],
             hints=missing, sh=cat(f"{ws}/link_dir/decoy.txt"),
             py=f"open({ws + '/link_dir/decoy.txt'!r}).read()"),
        dict(name="network_to_local_server", expected="allowed" if network else "denied",
             marker=t["net"], hints=neterr, sh=fetch_sh, py=fetch_py),
        dict(name="child_process_read_outside", expected="denied", marker=t["out"], hints=missing,
             sh=nested(nested(cat(fx.decoy))), py=f"sh_run(['cat', {fx.decoy!r}])"),
        dict(name="child_process_network", expected="allowed" if network else "denied",
             marker=t["net"], hints=neterr, sh=nested(nested(fetch_sh)),
             py=("sh_run([sys.executable, '-c', "
                 + repr("import urllib.request;print(urllib.request.urlopen(%r, timeout=5).read().decode())" % url)
                 + "])")),
        dict(name="host_environment_not_inherited", expected="denied", marker=t["env"],
             hints=("path=",), sh="env", py="'\\n'.join(k + '=' + v for k, v in os.environ.items())"),
    ]
    if not fx.links_created:
        for d in defs:
            if d["name"].startswith(("symlink_", "directory_link_")):
                d["skip"] = "the probe could not create a link here (no permission for links)"
    return defs


def _shell_script(defs: List[Dict[str, Any]]) -> str:
    lines = ["run_check() { out=$(sh -c \"$2\" 2>&1); rc=$?; "
             "printf '@@%s@@%s@@%s\\n' \"$1\" \"$rc\" \"$(printf '%s' \"$out\" | head -c 2000 | tr '\\n\\r' '  ')\"; }"]
    for d in defs:
        if not d.get("skip"):
            lines.append(f"run_check {shlex.quote(d['name'])} {shlex.quote(d['sh'])}")
    return "\n".join(lines)


def _python_script(defs: List[Dict[str, Any]]) -> str:
    lines = [
        "import os, subprocess, sys, urllib.request",
        "def sh_run(args):",
        "    r = subprocess.run(args, capture_output=True, text=True)",
        "    return (r.stdout + r.stderr) or 'no output'",
        "def run_check(name, fn):",
        "    try:",
        "        out, rc = fn(), 0",
        "    except BaseException as e:",
        "        out, rc = type(e).__name__ + ': ' + str(e), 1",
        "    print('@@%s@@%s@@%s' % (name, rc, str(out).replace(chr(10), ' ').replace(chr(13), ' ')[:2000]))",
    ]
    for d in defs:
        if not d.get("skip"):
            lines.append(f"run_check({d['name']!r}, lambda: {d['py']})")
    return "\n".join(lines)


def _parse(output: str) -> Dict[str, Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for line in (output or "").splitlines():
        if not line.startswith("@@"):
            continue
        parts = line.split("@@", 3)
        if len(parts) == 4:
            try:
                seen[parts[1]] = {"rc": int(parts[2]), "out": parts[3]}
            except ValueError:
                continue
    return seen


def _judge(defs: List[Dict[str, Any]], seen: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for d in defs:
        row: Dict[str, Any] = {"check": d["name"], "expected": d["expected"]}
        if d.get("skip"):
            row.update(observed="inconclusive", ok=None, evidence=d["skip"])
        elif d["name"] not in seen:
            row.update(observed="inconclusive", ok=None, evidence="the check produced no result")
        else:
            rc, out = seen[d["name"]]["rc"], seen[d["name"]]["out"]
            if d["marker"] in out:
                observed = "allowed"
            elif rc == 127 and "sh" in d and "not found" in out.lower() and "python3" in d["sh"]:
                observed = "inconclusive"     # the image has no python3 for this check
            elif d.get("hints") and not any(h in out.lower() for h in d["hints"]):
                # Denied for no recognisable reason: the check itself may be broken.
                observed = "inconclusive"
            else:
                observed = "denied"
            row["observed"] = observed
            row["ok"] = None if observed == "inconclusive" else observed == d["expected"]
            if observed == "inconclusive":
                row["evidence_note"] = "neither the marker nor a recognisable refusal was seen"
            # Never echo a marker back; the evidence is the shape of the outcome.
            shown = out.replace(d["marker"], "<marker>")
            row["evidence"] = f"rc={rc}; {shown[:160]}"
        rows.append(row)
    return rows


def _status(rows: List[Dict[str, Any]]) -> str:
    if any(r["ok"] is False for r in rows):
        return "failed"
    if any(r["ok"] is None for r in rows):
        return "inconclusive" if all(r["ok"] is None for r in rows) else "verified_with_gaps"
    return "verified"


# ── operations ─────────────────────────────────────────────────────────────

def _container_spec(root: str, run_id: str):
    from src import execution_router, sandbox_exec
    from src.constants import ARTIFACT_RUNS_DIR
    from src.contracts import ExecutionSpec
    decision = execution_router.choose(
        sandbox_exec.manifest(), workspace=root, artifacts_root=ARTIFACT_RUNS_DIR,
        run_id=run_id, prefer="docker_workspace")
    if not decision.ok:
        return None, f"{decision.reason}: {decision.detail}"
    body = decision.spec.to_dict()
    body["limits"] = {**body["limits"], "seconds": 120, "memory_mb": sandbox_exec.memory_mb()}
    return ExecutionSpec.parse(body), ""


def _probe_container_op(op: str, fx: Fixture, image: str) -> Dict[str, Any]:
    from src import sandbox_exec
    from src.execution_backends import DockerWorkspaceBackend

    backend = DockerWorkspaceBackend(image=image)
    ready = backend.probe()
    if not ready["ok"]:
        return {"status": "unavailable", "reason": f"{ready['reason']}: {ready['detail']}", "checks": []}
    spec, why = _container_spec(fx.root, f"sbx-probe-{op}-{secrets.token_hex(3)}")
    if spec is None:
        return {"status": "unavailable", "reason": why, "checks": []}
    network = sandbox_exec.network()
    defs = _check_defs(fx, workspace="/workspace", network=network, workspace_writable=True,
                       inside_readable=True, host=fx.host_address_from_container())
    if op == "shell":
        argv = ["/bin/sh", "-c", _shell_script(defs)]
    else:
        argv = ["python", "-I", "-c", _python_script(defs)]
    result = backend.run(spec, argv, run_id=f"sbx-probe-{op}-{secrets.token_hex(3)}")
    if result.status not in ("completed", "failed"):
        return {"status": "unavailable", "reason": f"{result.status}: {result.reason}", "checks": []}
    rows = _judge(defs, _parse(result.stdout_tail))
    return {"status": _status(rows), "checks": rows, "image": image,
            "network_granted": network, "workspace_access": "read_write"}


async def _probe_code_mode(fx: Fixture, image: str) -> Dict[str, Any]:
    from src.code_mode import confined
    from src.code_mode.runner import run_code_mode

    access = confined.resolve_workspace_access()
    network = confined.resolve_network()
    runtime = confined.resolve_runtime()
    defs = _check_defs(fx, workspace="/workspace", network=network,
                       workspace_writable=access == "read_write",
                       inside_readable=access != "none", host=fx.host_address_from_container())
    note = ""
    if runtime == "host":
        note = ("code_mode.runtime is 'host' in settings: the probe still checks the confined "
                "runtime, but real run_code calls are NOT confined while that setting stands")
    result = await run_code_mode(_python_script(defs), runtime="confined", workspace=fx.root,
                                 image=image)
    if result.get("refused") or (result.get("runtime_guarantees") or {}).get("mode") != "container":
        return {"status": "unavailable", "reason": str(result.get("error") or "not started"),
                "checks": [], "configured_runtime": runtime}
    rows = _judge(defs, _parse(result.get("output") or ""))
    out = {"status": _status(rows), "checks": rows, "configured_runtime": runtime,
           "network_granted": network, "workspace_access": access,
           "runtime_guarantees": result.get("runtime_guarantees")}
    if note:
        out["note"] = note
        if out["status"] == "verified":
            out["status"] = "verified_but_not_in_effect"
    return out


def _probe_files(fx: Fixture) -> Dict[str, Any]:
    """The file tools' path policy (host, not a container)."""
    from src.tool_execution import _resolve_tool_path_in_roots

    def attempt(raw: str) -> str:
        try:
            _resolve_tool_path_in_roots([fx.root], raw, fx.root)
            return "allowed"
        except ValueError as exc:
            return f"denied ({str(exc)[:80]})"
        except Exception as exc:  # noqa: BLE001
            return f"denied ({type(exc).__name__})"

    cases = [
        ("read_inside", "allowed", fx.allowed),
        ("relative_inside", "allowed", "allowed.txt"),
        ("outside_absolute_path", "denied", fx.decoy),
        ("outside_relative_path", "denied", "../outside/decoy.txt"),
        ("sibling_sharing_the_root_prefix", "denied", fx.secret),
        ("sibling_relative", "denied", "../root2/secret.txt"),
        ("symlink_absolute_does_not_extend_access", "denied", fx.link_abs),
        ("symlink_relative_does_not_extend_access", "denied", fx.link_rel),
        ("directory_link_does_not_extend_access", "denied", os.path.join(fx.link_dir, "decoy.txt")),
    ]
    rows = []
    for name, expected, raw in cases:
        if name.startswith(("symlink_", "directory_link_")) and not fx.links_created:
            rows.append({"check": name, "expected": expected, "observed": "inconclusive", "ok": None,
                         "evidence": "the probe could not create a link here"})
            continue
        got = attempt(raw)
        observed = "allowed" if got == "allowed" else "denied"
        rows.append({"check": name, "expected": expected, "observed": observed,
                     "ok": observed == expected, "evidence": got})
    return {"status": _status(rows), "checks": rows,
            "enforcement": "host_path_policy", "container": False}


async def _probe_descendants(fx: Fixture, image: str) -> Dict[str, Any]:
    from src.code_mode import confined
    from src.code_mode.runner import run_code_mode

    code = ("import subprocess, time\n"
            "subprocess.Popen(['sleep', '300'])\n"
            "time.sleep(300)\n")
    task = asyncio.ensure_future(run_code_mode(code, runtime="confined", workspace=fx.root, image=image))

    async def running() -> List[str]:
        done = await asyncio.to_thread(
            subprocess.run, ["docker", "ps", "-a", "--filter", f"name={confined._CONTAINER_PREFIX}",
                             "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
        return [n for n in done.stdout.split() if n]

    started = False
    for _ in range(40):
        if task.done():
            break
        if await running():
            started = True
            break
        await asyncio.sleep(0.25)
    if not started:
        result = task.result() if task.done() else None
        task.cancel()
        reason = str((result or {}).get("error") or "the guest container never started")
        return {"status": "unavailable", "reason": reason, "checks": []}
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    left = await running()
    row = {"check": "descendant_dies_with_container_on_cancel", "expected": "denied",
           "observed": "allowed" if left else "denied", "ok": not left,
           "evidence": f"containers left: {len(left)}"}
    return {"status": _status([row]), "checks": [row]}


# ── entry point ────────────────────────────────────────────────────────────

def _route_for(op: str, described: Dict[str, Any]) -> str:
    if op in ("shell", "python"):
        return str(described.get("target") or "host")
    if op == "code_mode":
        from src.code_mode import confined
        return "container" if confined.resolve_runtime() == "confined" else "host"
    if op == "files":
        return "host_path_policy"
    return "container" if _route_for("code_mode", described) == "container" else "host"


async def run_sandbox_probe(operations: Optional[List[str]] = None, *,
                            image: Optional[str] = None) -> Dict[str, Any]:
    """Run the probe. ``operations`` defaults to every one in ``OPERATIONS``."""
    from src import sandbox_exec

    wanted = [op for op in (operations or OPERATIONS) if op in OPERATIONS]
    unknown = [op for op in (operations or []) if op not in OPERATIONS]
    image = image or sandbox_exec.image()
    described = sandbox_exec.describe()
    report: Dict[str, Any] = {
        "host": _host_kind(),
        "configured": {"sandbox": described, "image": image},
        "operations": {},
        "coverage": coverage_for_host(),
        "unknown_operations": unknown,
        "started_at": time.time(),
    }
    fx = await asyncio.to_thread(Fixture)
    try:
        for op in wanted:
            started = time.monotonic()
            try:
                if op in ("shell", "python"):
                    body = await asyncio.to_thread(_probe_container_op, op, fx, image)
                elif op == "code_mode":
                    body = await _probe_code_mode(fx, image)
                elif op == "files":
                    body = await asyncio.to_thread(_probe_files, fx)
                else:
                    body = await _probe_descendants(fx, image)
            except Exception as exc:  # noqa: BLE001 - a probe never raises
                logger.exception("sandbox probe %s failed", op)
                body = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}", "checks": []}
            body["route_for_real_calls"] = _route_for(op, described)
            body["in_effect"] = body["status"].startswith("verified") and body["status"] != (
                "verified_but_not_in_effect") and body["route_for_real_calls"] in (
                    "container", "host_path_policy")
            body["duration_ms"] = int((time.monotonic() - started) * 1000)
            report["operations"][op] = body
    finally:
        await asyncio.to_thread(fx.close)
    statuses = {op: b["status"] for op, b in report["operations"].items()}
    report["summary"] = statuses
    report["ok"] = bool(statuses) and all(s.startswith("verified") for s in statuses.values())
    report["failed"] = sorted(op for op, s in statuses.items() if s == "failed")
    report["unavailable"] = sorted(op for op, s in statuses.items() if s == "unavailable")
    report["finished_at"] = time.time()
    return report


def render_text(report: Dict[str, Any]) -> str:
    """Compact human/model-readable form of a probe report."""
    lines = [f"Sandbox probe on a {report.get('host')} host "
             f"(image {report['configured'].get('image')}): "
             + ("every probed operation verified" if report.get("ok") else "NOT all verified")]
    for op, body in report.get("operations", {}).items():
        lines.append(f"- {op}: {body['status']} (real calls route: {body['route_for_real_calls']}, "
                     f"in effect: {body['in_effect']})"
                     + (f" — {body['reason']}" if body.get("reason") else ""))
        for row in body.get("checks", []):
            if row.get("ok") is not True:
                lines.append(f"    {row['check']}: expected {row['expected']}, "
                             f"observed {row['observed']} ({row.get('evidence', '')})")
    lines.append("Coverage on this host:")
    for op, text in (report.get("coverage") or {}).items():
        lines.append(f"- {op}: {text}")
    return "\n".join(lines)


def probe_as_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
