"""ui_smoke.py — harness-driven UI smoke test (lote H1).

The forensic case this exists for (`silhouettes_analysis.md`, chat `782b7d89`,
16-09-2026): a turn rewrote `templates/editor.html` + `static/editor/*`,
170 pytest tests + 20 node tests passed, and the harness sealed the turn
`verified`. Nothing in the browser worked: Flask served the new `.mjs`
modules with `Content-Type: text/plain`, the browser refused to execute
them as ES modules, and not a single button in the UI ever ran. The bug was
invisible to every unit test — it only shows up as a response header, and a
27B model never opened a browser to see it. Luis found it by hand; the
harness had no mechanism that could have found it first.

This module is that mechanism, and it does not trust the model to run it:
given a workspace and the files a turn mutated, it decides on its own
whether a web app is in play, starts the app's own server (never the model's
foreground turn — this always runs detached and is always killed in
`finally`), fetches the pages a Flask/FastAPI/static app would actually
serve, and — the check that would have caught the real bug — re-fetches
every `<script src>` / `<link href>` / ES `import` target and checks both
its HTTP status AND its `Content-Type`. A `.js`/`.mjs` asset that comes back
as anything other than a JS mime type is `ok=False, problem="content_type"`,
whether or not a single pytest ran.

When Python `playwright` is installed with a Chromium build available, the
page is also loaded headless and real console errors / uncaught exceptions /
4xx-5xx network responses are captured — closer still to what Luis actually
saw ("I ran app.py and nothing works"). Without it, the module degrades to
the HTTP-only checks above; it never fails a turn for lacking Playwright.

Public entry point: `run_for_turn(workspace, mutated_paths)` — see its
docstring. Settings: `agent_ui_smoke` (default True), and
`agent_ui_smoke_timeout_seconds` (default 60).

Stdlib + `urllib` for the HTTP checks; `playwright` (Python) is optional and
imported lazily. Never raises — every failure mode is folded into the
returned report so the calling loop can always read `report["ok"]`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from core.platform_compat import IS_WINDOWS
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60
MAX_PAGES = 6
MAX_ASSETS = 25
FETCH_TIMEOUT_S = 6

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# trigger: does this turn need a UI smoke at all?
# ---------------------------------------------------------------------------

_UI_MUTATION_RE = re.compile(
    r"(?:^|/)(?:static|templates|public|dist)/"
    r"|\.(?:html?|css|m?jsx?|tsx?)$"
    r"|(?:^|/)index\.html$"
    r"|(?:^|/)(?:app|main|server|wsgi|asgi)\.py$"
    r"|(?:^|/)package\.json$",
    re.I,
)


def _is_ui_mutation(path: str) -> bool:
    rel = (path or "").replace("\\", "/")
    return bool(_UI_MUTATION_RE.search(rel))


# ---------------------------------------------------------------------------
# server detection
# ---------------------------------------------------------------------------

def _read_text(path: str, cap: int = 400_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


def _find_index_html_dir(workspace: str, max_depth: int = 2) -> Optional[str]:
    """The shallowest directory under `workspace` (workspace itself first)
    that holds an `index.html`, skipping the usual noise directories."""
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build"}
    root_depth = workspace.rstrip("/\\").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(workspace):
        depth = dirpath.rstrip("/\\").count(os.sep) - root_depth
        if depth > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if "index.html" in filenames:
            return dirpath
    return None


def detect_server(workspace: str) -> Optional[Dict[str, Any]]:
    """First match wins: Flask app.py, FastAPI (uvicorn) app.py, an npm
    start/dev script, or a static `index.html` served with `http.server`.

    Returns a spec dict consumed by `_build_launch`; None when nothing in
    the workspace looks like a web app at all."""
    if not workspace or not os.path.isdir(workspace):
        return None

    for entry in ("app.py", "wsgi.py", "asgi.py", "main.py", "server.py"):
        p = os.path.join(workspace, entry)
        if not os.path.isfile(p):
            continue
        src = _read_text(p)
        if re.search(r"\bFlask\s*\(", src):
            return {"kind": "flask", "entry": entry, "source": src}
        if re.search(r"\bFastAPI\s*\(", src):
            m = re.search(r"^(\w+)\s*=\s*FastAPI\s*\(", src, re.M)
            var = m.group(1) if m else "app"
            return {"kind": "fastapi", "entry": entry, "source": src, "asgi_var": var}

    pkg_path = os.path.join(workspace, "package.json")
    if os.path.isfile(pkg_path):
        try:
            data = json.loads(_read_text(pkg_path) or "{}")
        except ValueError:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            for name in ("start", "dev"):
                script = scripts.get(name)
                if isinstance(script, str) and script.strip():
                    return {"kind": "npm", "script": name, "source": ""}

    static_dir = _find_index_html_dir(workspace)
    if static_dir:
        return {"kind": "static", "dir": static_dir, "source": ""}

    return None


def _project_python(workspace: str) -> str:
    try:
        from src.agent_tools.subprocess_tools import project_python
        return project_python(workspace, native_host_environment())
    except Exception:  # pragma: no cover - defensive
        return sys.executable or "python"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _build_launch(spec: Dict[str, Any], workspace: str, port: int,
                   python_exe: str) -> Tuple[List[str], Dict[str, str], str]:
    """(argv, extra env, cwd) for `subprocess.Popen`. Raises ValueError for an
    unknown spec kind (should never happen — `detect_server` controls kind)."""
    kind = spec.get("kind")
    env_extra = {"PORT": str(port), "FLASK_RUN_PORT": str(port)}
    if kind == "flask":
        entry = spec["entry"]
        if re.search(r"\bapp\.run\s*\(", spec.get("source") or ""):
            # The app reads its own port; PORT/FLASK_RUN_PORT above is the
            # best we can do generically, but most templates honor it.
            return [python_exe, entry], env_extra, workspace
        flask_exe = shutil.which("flask")
        env_extra["FLASK_APP"] = entry
        if flask_exe:
            return [flask_exe, "run", "--port", str(port)], env_extra, workspace
        return [python_exe, "-m", "flask", "run", "--port", str(port)], env_extra, workspace
    if kind == "fastapi":
        module = os.path.splitext(spec["entry"])[0]
        target = f"{module}:{spec.get('asgi_var') or 'app'}"
        return (
            [python_exe, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)],
            env_extra, workspace,
        )
    if kind == "npm":
        npm = shutil.which("npm.cmd") if IS_WINDOWS else shutil.which("npm")
        npm = npm or ("npm.cmd" if IS_WINDOWS else "npm")
        return [npm, "run", spec["script"]], env_extra, workspace
    if kind == "static":
        return [python_exe, "-m", "http.server", str(port)], {}, spec["dir"]
    raise ValueError(f"unknown ui_smoke server kind: {kind!r}")


# ---------------------------------------------------------------------------
# process lifecycle — detached launch, guaranteed kill
# ---------------------------------------------------------------------------

def _popen_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _kill_process_tree(proc: Optional["subprocess.Popen"]) -> None:
    """Kill `proc` and every descendant it spawned, best effort, never raises.

    Reuses `src/process_ownership.py::terminate_tree` — the same tree-kill
    `src/agent_tools/subprocess_tools.py::_kill_tree`/`_kill_tree_async` use
    for the agent's own background jobs (psutil-verified descendants on
    POSIX, `taskkill /T /F` on Windows when psutil is unavailable). We hold
    the just-spawned `Popen` object ourselves, so `unverified_tree_ok=True`
    is safe the same way it is there: the pid was never released back to the
    OS between our own spawn and this kill.
    """
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        from src import process_ownership
        pgid = process_ownership.process_group_id(pid) if not IS_WINDOWS else None
        process_ownership.terminate_tree(pid, pgid=pgid, unverified_tree_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("[ui_smoke] process_ownership tree kill failed for pid %s: %s", pid, e)
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP probing
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: float = FETCH_TIMEOUT_S) -> Tuple[Optional[int], Optional[str], bytes]:
    """GET `url`. Returns (status, content-type, body) — status is set even
    on a 4xx/5xx (that is data, not a failure of the probe itself). None
    status means the connection itself failed (server not up, DNS, etc)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Faustus-ui-smoke/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200_000)
            return resp.status, resp.headers.get("Content-Type"), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4_000)
        except Exception:
            body = b""
        ctype = e.headers.get("Content-Type") if e.headers else None
        return e.code, ctype, body
    except Exception:
        return None, None, b""


def _wait_ready(base_url: str, proc: "subprocess.Popen", timeout_s: float) -> Tuple[bool, str]:
    """Poll `GET base_url/` until it answers (any status) or `proc` exits."""
    deadline = time.time() + max(0.5, timeout_s)
    last_err = "timed out waiting for a response"
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                out, _ = proc.communicate(timeout=2)
                tail = (out or b"").decode("utf-8", "replace").strip()[-600:]
            except Exception:
                pass
            return False, f"process exited (code {proc.returncode}) before answering" + (f": {tail}" if tail else "")
        status, _ctype, _body = _fetch(base_url + "/", timeout=2)
        if status is not None:
            return True, ""
        time.sleep(0.25)
    return False, last_err


_ROUTE_RE = re.compile(r"@\w+\.route\(\s*['\"]([^'\"<>]+)['\"]")
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
_LINK_HREF_RE = re.compile(r"<link\b[^>]*\bhref=[\"']([^\"']+\.css[^\"']*)[\"']", re.I)
_ES_IMPORT_RE = re.compile(r"""\bimport(?:\s+[\w${}*,\s]+\s+from)?\s*['"]([^'"]+)['"]""")
_ES_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)""")

_JS_URL_RE = re.compile(r"\.m?js(?:\?.*)?$", re.I)
_CSS_URL_RE = re.compile(r"\.css(?:\?.*)?$", re.I)
_JS_MIME_OK = {
    "text/javascript", "application/javascript", "application/x-javascript",
    "application/ecmascript", "text/ecmascript", "module",
}


def _extract_asset_refs(html_or_js: str) -> List[str]:
    refs: List[str] = []
    refs += _SCRIPT_SRC_RE.findall(html_or_js)
    refs += _LINK_HREF_RE.findall(html_or_js)
    refs += _ES_IMPORT_RE.findall(html_or_js)
    refs += _ES_DYNAMIC_IMPORT_RE.findall(html_or_js)
    out: List[str] = []
    for r in refs:
        r = (r or "").strip()
        if not r or r.startswith(("http://", "https://", "//", "data:", "mailto:")):
            continue
        if r not in out:
            out.append(r)
    return out


def _check_asset(url: str) -> Dict[str, Any]:
    status, ctype, body = _fetch(url)
    ok = status is not None and status < 400
    problem = None
    mime = (ctype or "").split(";", 1)[0].strip().lower()
    if not ok:
        problem = "status"
    elif _JS_URL_RE.search(url):
        if mime not in _JS_MIME_OK:
            ok = False
            problem = "content_type"
    elif _CSS_URL_RE.search(url):
        if mime and mime != "text/css":
            ok = False
            problem = "content_type"
    return {"url": url, "status": status, "content_type": ctype, "ok": ok, "problem": problem}


def _crawl(spec: Dict[str, Any], base_url: str, deadline: float) -> Tuple[List[Dict], List[Dict], List[str]]:
    page_paths = ["/"]
    for m in _ROUTE_RE.finditer(spec.get("source") or ""):
        route = m.group(1)
        if route.startswith("/") and route not in page_paths and len(page_paths) < MAX_PAGES:
            page_paths.append(route)

    pages: List[Dict[str, Any]] = []
    assets: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []

    for path in page_paths:
        if time.time() > deadline:
            break
        url = base_url + path
        status, ctype, body = _fetch(url)
        ok = status is not None and status < 400
        pages.append({"url": url, "status": status, "ok": ok})
        if not ok:
            problems.append(f"page {url} -> status {status}")
            continue
        mime = (ctype or "").split(";", 1)[0].strip().lower()
        if mime and "html" not in mime:
            continue
        html = body.decode("utf-8", "replace")
        for ref in _extract_asset_refs(html)[:MAX_ASSETS]:
            if time.time() > deadline:
                break
            asset_url = urljoin(url, ref)
            if asset_url in assets:
                continue
            a = _check_asset(asset_url)
            assets[asset_url] = a
            if not a["ok"]:
                problems.append(f"asset {asset_url}: {a['problem']}")

    return pages, list(assets.values()), problems


# ---------------------------------------------------------------------------
# optional playwright pass
# ---------------------------------------------------------------------------

def playwright_available() -> bool:
    if not _truthy(_setting("agent_ui_smoke_playwright", True)):
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def _playwright_console_errors(base_url: str, timeout_s: float) -> List[str]:
    """Load `base_url/` headless and return console errors, uncaught page
    exceptions, and 4xx/5xx network responses as short strings. Raises on
    any Playwright/driver problem — the caller decides whether that is fatal
    (it is not: `run_smoke` swallows it and reports `playwright_used=False`).
    """
    from playwright.sync_api import sync_playwright  # local import: optional dep

    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isdir("/opt/pw-browsers"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"

    errors: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(f"uncaught exception: {exc}"))
            page.on("response", lambda resp: errors.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None)
            page.goto(base_url + "/", timeout=int(max(1.0, timeout_s) * 1000), wait_until="load")
            page.wait_for_timeout(500)
        finally:
            browser.close()
    return errors


# ---------------------------------------------------------------------------
# report shape + summary
# ---------------------------------------------------------------------------

def _report(*, ran: bool, ok: bool = True, summary: str = "", server_cmd: Optional[str] = None,
            url: Optional[str] = None, pages: Optional[List[Dict]] = None,
            assets: Optional[List[Dict]] = None, console_errors: Optional[List[str]] = None,
            playwright_used: bool = False) -> Dict[str, Any]:
    return {
        "ran": bool(ran),
        "ok": bool(ok),
        "summary": summary,
        "server_cmd": server_cmd,
        "url": url,
        "pages": pages or [],
        "assets": assets or [],
        "console_errors": console_errors or [],
        "playwright_used": bool(playwright_used),
    }


def _summarize(pages: List[Dict], assets: List[Dict], console_errors: List[str], problems: List[str]) -> str:
    if not problems and not console_errors:
        n_assets = len(assets)
        n_pages = len(pages)
        return f"ui_smoke ok: {n_pages} page(s), {n_assets} asset(s) checked, no console errors"
    bits = []
    bad_assets = [a for a in assets if not a.get("ok")]
    if bad_assets:
        sample = bad_assets[0]
        bits.append(
            f"{len(bad_assets)} asset(s) failed (e.g. {sample['url']}: "
            f"{sample.get('problem')}"
            + (f", content-type={sample.get('content_type')}" if sample.get("problem") == "content_type" else "")
            + ")"
        )
    bad_pages = [p for p in pages if not p.get("ok")]
    if bad_pages:
        bits.append(f"{len(bad_pages)} page(s) failed to load")
    if console_errors:
        bits.append(f"{len(console_errors)} browser console error(s) (e.g. {console_errors[0][:160]})")
    return "ui_smoke FAILED: " + "; ".join(bits)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def run_smoke(workspace: str, spec: Dict[str, Any], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Launch the detected server, probe it, kill it. Always returns
    `ran=True` — every failure mode (server never starts, times out, crashes)
    is folded into `ok=False` + `summary`, never an exception."""
    started = time.time()
    port = _free_port()
    python_exe = _project_python(workspace)
    try:
        argv, env_extra, cwd = _build_launch(spec, workspace, port, python_exe)
    except Exception as e:  # noqa: BLE001
        return _report(ran=True, ok=False, summary=f"could not build a launch command: {e}")

    env = native_host_environment(extra=env_extra)
    cmd_str = " ".join(argv)
    proc: Optional["subprocess.Popen"] = None
    try:
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **_popen_kwargs(),
            )
        except (OSError, FileNotFoundError) as e:
            return _report(ran=True, ok=False, server_cmd=cmd_str,
                            summary=f"failed to start server ({cmd_str}): {e}")

        base_url = f"http://127.0.0.1:{port}"
        ready_timeout = min(20.0, max(5.0, timeout_s * 0.4))
        ready, ready_err = _wait_ready(base_url, proc, ready_timeout)
        if not ready:
            return _report(ran=True, ok=False, server_cmd=cmd_str, url=base_url,
                            summary=f"server never answered on {base_url} within {ready_timeout:.0f}s: {ready_err}")

        deadline = started + timeout_s
        pages, assets, problems = _crawl(spec, base_url, deadline)

        console_errors: List[str] = []
        playwright_used = False
        if playwright_available():
            remaining = max(3.0, deadline - time.time())
            try:
                console_errors = _playwright_console_errors(base_url, remaining)
                playwright_used = True
            except Exception as e:  # noqa: BLE001
                logger.debug("[ui_smoke] playwright pass skipped: %s", e)

        ok = not problems and not console_errors
        summary = _summarize(pages, assets, console_errors, problems)
        return _report(ran=True, ok=ok, server_cmd=cmd_str, url=base_url, pages=pages,
                        assets=assets, console_errors=console_errors, summary=summary,
                        playwright_used=playwright_used)
    finally:
        _kill_process_tree(proc)


def run_for_turn(workspace: str, mutated_paths: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Harness entry point, called once per turn next to
    `project_tests.run_for_turn` (see `docs/spec/ui_smoke.md` /
    `H1_wiring.md` for exactly where).

    Returns `None` when the feature is switched off (`agent_ui_smoke`).
    Otherwise always returns a report dict with `ran` set: `False` when
    nothing in `mutated_paths` is UI-facing or no server could be detected
    in `workspace` (nothing to smoke-test, not a failure), `True` once a
    server was actually launched and probed."""
    if not bool(_setting("agent_ui_smoke", True)):
        return None
    if not workspace or not os.path.isdir(workspace):
        return _report(ran=False, summary="no workspace to test")

    mutated = list(mutated_paths or [])
    if not any(_is_ui_mutation(p) for p in mutated):
        return _report(ran=False, summary="no UI-facing files changed this turn; ui_smoke skipped")

    spec = detect_server(workspace)
    if not spec:
        return _report(ran=False, summary="no web server (Flask/FastAPI/npm/static) detected; ui_smoke skipped")

    timeout_s = float(_setting("agent_ui_smoke_timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    return run_smoke(workspace, spec, timeout_s=timeout_s)


def failure_message(report: Dict[str, Any]) -> str:
    """Bounded fix-round instruction for the model, mirroring
    `project_tests.failure_message`'s shape."""
    lines = [
        "[Harness check — automatic message from the runtime, not from the user]",
        "ui_smoke started your app's own server and fetched the pages/assets it serves; "
        f"it found a problem BEFORE any human opened a browser: {report.get('summary')}",
    ]
    bad_assets = [a for a in (report.get("assets") or []) if not a.get("ok")][:6]
    for a in bad_assets:
        extra = f" (Content-Type: {a.get('content_type')})" if a.get("problem") == "content_type" else ""
        lines.append(f"- {a['url']}: {a.get('problem')}{extra}")
    for e in (report.get("console_errors") or [])[:6]:
        lines.append(f"- console: {e}")
    lines.append(
        "If this is a Content-Type problem on a static asset (the classic case: a `.mjs`/`.js` "
        "file served as text/plain so the browser refuses to run it as a module), fix the server's "
        "MIME mapping — do not rename the file or add a workaround in the HTML. Then it will be "
        "re-checked automatically; you do not need to re-run this yourself."
    )
    return "\n".join(lines)


def compact(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Small shape for persistence/UI: drop full page/console bodies."""
    if not report:
        return None
    out = {
        "ran": report.get("ran"),
        "ok": report.get("ok"),
        "summary": report.get("summary"),
        "url": report.get("url"),
        "playwright_used": report.get("playwright_used"),
        "pages": len(report.get("pages") or []),
        "assets": len(report.get("assets") or []),
        "assets_failed": len([a for a in (report.get("assets") or []) if not a.get("ok")]),
        "console_errors": len(report.get("console_errors") or []),
    }
    return out
