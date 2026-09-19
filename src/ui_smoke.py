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
    # `templates/` holds Jinja sources, not a static site: serving it with
    # http.server yields a 200 page whose /static/* links all 404 (live 17-09).
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", "templates"}
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


_LOCAL_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)


def _local_imports_source(workspace: str, src: str, cap: int = 6) -> str:
    """Source of up to `cap` first-party modules the entry imports
    (`pkg.mod` -> pkg/mod.py or pkg/__init__.py under the workspace)."""
    out: List[str] = []
    for m in _LOCAL_IMPORT_RE.finditer(src or ""):
        dotted = (m.group(1) or m.group(2) or "").strip()
        if not dotted or dotted.split(".")[0] in ("flask", "fastapi", "os", "sys", "json", "re"):
            continue
        rel = dotted.replace(".", os.sep)
        for cand in (rel + ".py", os.path.join(rel, "__init__.py")):
            path = os.path.join(workspace, cand)
            if os.path.isfile(path):
                out.append(_read_text(path))
                break
        if len(out) >= cap:
            break
    return "\n".join(out)


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
        # The entry often only does `from notas.api import create_app` — the
        # `Flask(` call lives one import away (live 17-09). Look there too.
        joined = src + "\n" + _local_imports_source(workspace, src)
        if re.search(r"\bFlask\s*\(", joined) or re.search(r"^\s*(?:from|import)\s+flask\b", joined, re.M):
            return {"kind": "flask", "entry": entry, "source": src}
        if re.search(r"\bFastAPI\s*\(", joined):
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
        # Always `flask run --port N` with FLASK_APP=<entry>: the module's
        # `app` / `create_app` is discovered by Flask itself, and a hard-coded
        # `app.run(port=5055)` in the entry is ignored (live 17-09: running
        # `python app.py` left the smoke probing a port the app never bound).
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


# In-page accessibility audit, run via `page.evaluate`. Deterministic,
# dependency-free (no axe-core): each finding is
# {rule, severity: "serious"|"moderate"|"minor", selector, detail}. Findings
# are capped at 10 per rule for the returned list; `counts_by_severity` and
# `counts_by_rule` are computed from the *full*, uncapped pass so nothing is
# silently lost, only the verbose listing is bounded.
_A11Y_JS = r"""
() => {
  const findings = [];
  const MAX_PER_RULE = 10;
  const ruleCounts = {};

  function push(rule, severity, el, detail) {
    ruleCounts[rule] = (ruleCounts[rule] || 0) + 1;
    findings.push({ rule, severity, selector: selectorFor(el), detail });
  }

  function selectorFor(el, maxDepth) {
    maxDepth = maxDepth || 4;
    if (!el || el.nodeType !== 1) return '';
    const path = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < maxDepth) {
      let part = node.tagName ? node.tagName.toLowerCase() : '';
      if (node.id) {
        part += '#' + node.id;
        path.unshift(part);
        break;
      }
      if (typeof node.className === 'string' && node.className.trim()) {
        const cls = node.className.trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
        if (cls) part += '.' + cls;
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.prototype.filter.call(parent.children, (c) => c.tagName === node.tagName);
        if (siblings.length > 1) {
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
      }
      path.unshift(part);
      node = parent;
      depth++;
    }
    return path.join(' > ');
  }

  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function getAccessibleName(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const txt = labelledby.split(/\s+/).map((id) => {
        const t = document.getElementById(id);
        return t ? t.textContent.trim() : '';
      }).join(' ').trim();
      if (txt) return txt;
    }
    const imgWithAlt = el.querySelector ? el.querySelector('img[alt]') : null;
    if (imgWithAlt && (imgWithAlt.getAttribute('alt') || '').trim()) return imgWithAlt.getAttribute('alt').trim();
    const text = (el.textContent || '').trim();
    if (text) return text;
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return '';
  }

  function hasRealLabel(el) {
    try {
      if (el.labels && el.labels.length > 0) return true;
    } catch (e) { /* ignore */ }
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return true;
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const ok = labelledby.split(/\s+/).some((id) => {
        const t = document.getElementById(id);
        return t && t.textContent.trim();
      });
      if (ok) return true;
    }
    return false;
  }

  // 1. img without alt attribute (serious). alt="" is a valid "decorative"
  // marker and is intentionally not flagged.
  document.querySelectorAll('img').forEach((img) => {
    if (!img.hasAttribute('alt')) push('img-alt', 'serious', img, 'img element has no alt attribute');
  });

  // 2. interactive controls without an accessible name (serious)
  document.querySelectorAll('button, [role="button"], a[href]').forEach((el) => {
    if (!isVisible(el)) return;
    const name = getAccessibleName(el);
    if (!name) push('name-missing', 'serious', el, (el.tagName || '').toLowerCase() + ' has no accessible name');
  });

  // 3. form controls without a label (serious; placeholder-only is minor)
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].indexOf(type) !== -1) return;
    if (!isVisible(el)) return;
    if (hasRealLabel(el)) return;
    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) {
      push('input-label', 'minor', el, 'only has a placeholder, no real label');
    } else {
      push('input-label', 'serious', el, 'no label, aria-label or aria-labelledby');
    }
  });

  // 4. <html> without lang (moderate)
  const htmlEl = document.documentElement;
  const lang = htmlEl.getAttribute('lang');
  if (!lang || !lang.trim()) push('html-lang', 'moderate', htmlEl, 'html element is missing a lang attribute');

  // 5. missing/empty <title> (moderate)
  const titleEl = document.querySelector('title');
  if (!titleEl || !(titleEl.textContent || '').trim()) {
    push('page-title', 'moderate', document.head || htmlEl, 'document is missing a non-empty <title>');
  }

  // 6. duplicate id attributes (moderate)
  const idCounts = {};
  document.querySelectorAll('[id]').forEach((el) => {
    const id = el.id;
    if (!id) return;
    idCounts[id] = (idCounts[id] || 0) + 1;
  });
  Object.keys(idCounts).forEach((id) => {
    if (idCounts[id] > 1) {
      push('duplicate-id', 'moderate', document.getElementById(id), 'id "' + id + '" is used ' + idCounts[id] + ' times');
    }
  });

  // 7. heading level skips, e.g. h2 -> h4 (minor)
  const headings = Array.prototype.slice.call(document.querySelectorAll('h1, h2, h3, h4, h5, h6'));
  let prevLevel = 0;
  headings.forEach((h) => {
    const level = parseInt(h.tagName.substring(1), 10);
    if (prevLevel && level > prevLevel + 1) {
      push('heading-skip', 'minor', h, 'heading level jumps from h' + prevLevel + ' to h' + level);
    }
    prevLevel = level;
  });

  // 8. text color contrast below WCAG AA (4.5:1 normal, 3:1 for >=24px or
  // >=18.66px bold), computed from computed styles, walking up for the
  // first non-transparent background.
  function parseColor(str) {
    if (!str) return null;
    const m = str.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(',').map((s) => parseFloat(s));
    if (parts.length < 3 || parts.some((v) => Number.isNaN(v))) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  }
  function relLuminance(r, g, b) {
    function c(v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b);
  }
  function contrastRatio(a, b) {
    const l1 = relLuminance(a.r, a.g, a.b) + 0.05;
    const l2 = relLuminance(b.r, b.g, b.b) + 0.05;
    return l1 > l2 ? l1 / l2 : l2 / l1;
  }
  function effectiveBackground(el) {
    let node = el;
    while (node) {
      const style = getComputedStyle(node);
      const parsed = parseColor(style.backgroundColor);
      if (parsed && parsed.a > 0) return parsed;
      node = node.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }

  const textEls = document.body ? document.body.querySelectorAll('*') : [];
  let contrastChecked = 0;
  textEls.forEach((el) => {
    if (contrastChecked > 400) return; // safety cap for very large pages
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'title') return;
    let hasDirectText = false;
    for (let i = 0; i < el.childNodes.length; i++) {
      const n = el.childNodes[i];
      if (n.nodeType === 3 && n.textContent && n.textContent.trim()) { hasDirectText = true; break; }
    }
    if (!hasDirectText) return;
    if (!isVisible(el)) return;
    contrastChecked++;
    const style = getComputedStyle(el);
    const color = parseColor(style.color);
    if (!color || color.a === 0) return;
    const bg = effectiveBackground(el);
    const ratio = contrastRatio(color, bg);
    const fontSize = parseFloat(style.fontSize) || 16;
    const fontWeight = parseInt(style.fontWeight, 10) || 400;
    const isLarge = fontSize >= 24 || (fontSize >= 18.66 && fontWeight >= 700);
    const threshold = isLarge ? 3.0 : 4.5;
    if (ratio < threshold) {
      const severity = ratio < 3.0 ? 'serious' : 'moderate';
      push('color-contrast', severity, el, 'contrast ratio ' + ratio.toFixed(2) + ':1 (needs ' + threshold.toFixed(1) + ':1)');
    }
  });

  const byRule = {};
  findings.forEach((f) => {
    byRule[f.rule] = byRule[f.rule] || [];
    byRule[f.rule].push(f);
  });
  const capped = [];
  Object.keys(byRule).forEach((rule) => {
    byRule[rule].slice(0, MAX_PER_RULE).forEach((f) => capped.push(f));
  });

  const countsBySeverity = { serious: 0, moderate: 0, minor: 0 };
  findings.forEach((f) => {
    if (countsBySeverity[f.severity] !== undefined) countsBySeverity[f.severity]++;
  });

  return { findings: capped, counts_by_severity: countsBySeverity, counts_by_rule: ruleCounts };
}
"""

# Navigation timing, LCP (via a buffered PerformanceObserver) and JS/CSS
# transfer size, read once the page has settled. Warnings only — perf never
# fails a turn (see `_perf_warnings`).
_PERF_JS = r"""
async () => {
  const nav = performance.getEntriesByType('navigation')[0] || null;
  const domContentLoaded = nav ? nav.domContentLoadedEventEnd : null;
  const load = nav ? nav.loadEventEnd : null;

  let lcp = 0;
  try {
    await new Promise((resolve) => {
      try {
        const po = new PerformanceObserver((list) => {
          const entries = list.getEntries();
          const last = entries[entries.length - 1];
          if (last) lcp = last.startTime;
        });
        po.observe({ type: 'largest-contentful-paint', buffered: true });
      } catch (e) { /* LCP not supported by this engine */ }
      setTimeout(resolve, 200);
    });
  } catch (e) { /* ignore */ }

  const resources = performance.getEntriesByType('resource') || [];
  let jsBytes = 0;
  let cssBytes = 0;
  resources.forEach((r) => {
    const size = r.transferSize || 0;
    const name = r.name || '';
    const initiator = r.initiatorType || '';
    if (initiator === 'script' || /\.m?js(\?.*)?$/i.test(name)) jsBytes += size;
    else if (/\.css(\?.*)?$/i.test(name)) cssBytes += size;
  });

  return {
    dom_content_loaded_ms: domContentLoaded,
    load_ms: load,
    lcp_ms: lcp,
    js_bytes: jsBytes,
    css_bytes: cssBytes,
    request_count: resources.length,
  };
}
"""

# perf warning thresholds — informational only, never gate a turn.
_PERF_LCP_MS_WARN = 2500
_PERF_LOAD_MS_WARN = 4000
_PERF_JS_BYTES_WARN = 1_500_000


def _perf_warnings(perf: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    lcp = perf.get("lcp_ms")
    if isinstance(lcp, (int, float)) and lcp > _PERF_LCP_MS_WARN:
        warnings.append(f"LCP {lcp:.0f}ms exceeds {_PERF_LCP_MS_WARN}ms")
    load = perf.get("load_ms")
    if isinstance(load, (int, float)) and load > _PERF_LOAD_MS_WARN:
        warnings.append(f"load {load:.0f}ms exceeds {_PERF_LOAD_MS_WARN}ms")
    js_bytes = perf.get("js_bytes")
    if isinstance(js_bytes, (int, float)) and js_bytes > _PERF_JS_BYTES_WARN:
        warnings.append(f"JS transfer {js_bytes / 1_000_000:.2f}MB exceeds {_PERF_JS_BYTES_WARN / 1_000_000:.1f}MB")
    return warnings


def _playwright_audit(base_url: str, timeout_s: float) -> Dict[str, Any]:
    """Load `base_url/` headless once and gather everything the Playwright
    pass produces: console errors / uncaught exceptions / 4xx-5xx network
    responses, the a11y audit (`_A11Y_JS`), and the perf snapshot
    (`_PERF_JS`). Raises on any Playwright/driver problem — the caller
    decides whether that is fatal (it is not: `run_smoke` swallows it and
    reports `playwright_used=False`).
    """
    from playwright.sync_api import sync_playwright  # local import: optional dep

    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isdir("/opt/pw-browsers"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"

    errors: List[str] = []
    a11y: Dict[str, Any] = {"findings": [], "counts_by_severity": {"serious": 0, "moderate": 0, "minor": 0}}
    perf: Dict[str, Any] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(f"uncaught exception: {exc}"))
            page.on("response", lambda resp: errors.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None)
            page.goto(base_url + "/", timeout=int(max(1.0, timeout_s) * 1000), wait_until="load")
            page.wait_for_timeout(500)
            try:
                a11y_raw = page.evaluate(_A11Y_JS)
                if isinstance(a11y_raw, dict):
                    a11y = {
                        "findings": a11y_raw.get("findings") or [],
                        "counts_by_severity": a11y_raw.get("counts_by_severity")
                        or {"serious": 0, "moderate": 0, "minor": 0},
                        "counts_by_rule": a11y_raw.get("counts_by_rule") or {},
                    }
            except Exception as e:  # noqa: BLE001
                logger.debug("[ui_smoke] a11y audit failed: %s", e)
            try:
                perf_raw = page.evaluate(_PERF_JS)
                if isinstance(perf_raw, dict):
                    perf = dict(perf_raw)
                    perf["warnings"] = _perf_warnings(perf)
            except Exception as e:  # noqa: BLE001
                logger.debug("[ui_smoke] perf audit failed: %s", e)
        finally:
            browser.close()
    return {"console_errors": errors, "a11y": a11y, "perf": perf}


# ---------------------------------------------------------------------------
# report shape + summary
# ---------------------------------------------------------------------------

def _report(*, ran: bool, ok: bool = True, summary: str = "", server_cmd: Optional[str] = None,
            url: Optional[str] = None, pages: Optional[List[Dict]] = None,
            assets: Optional[List[Dict]] = None, console_errors: Optional[List[str]] = None,
            playwright_used: bool = False, a11y: Optional[Dict[str, Any]] = None,
            perf: Optional[Dict[str, Any]] = None,
            quality_warnings: Optional[List[str]] = None) -> Dict[str, Any]:
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
        "a11y": a11y,
        "perf": perf,
        "quality_warnings": quality_warnings or [],
    }


def _quality_warnings(a11y: Optional[Dict[str, Any]], perf: Optional[Dict[str, Any]]) -> List[str]:
    """Human-readable warnings for findings that never fail a turn on their
    own: a11y (regardless of severity — blocking is a separate decision made
    by the caller) and perf thresholds. Surfaced to the completion gate as
    `quality_warnings`, distinct from `ok`."""
    out: List[str] = []
    if a11y:
        counts = a11y.get("counts_by_severity") or {}
        serious = int(counts.get("serious") or 0)
        moderate = int(counts.get("moderate") or 0)
        minor = int(counts.get("minor") or 0)
        if serious or moderate or minor:
            out.append(f"a11y: {serious} serious, {moderate} moderate, {minor} minor finding(s)")
    if perf:
        for w in perf.get("warnings") or []:
            out.append(f"perf: {w}")
    return out


def _summarize(pages: List[Dict], assets: List[Dict], console_errors: List[str], problems: List[str],
                a11y: Optional[Dict[str, Any]] = None, perf: Optional[Dict[str, Any]] = None,
                a11y_blocking_failed: bool = False) -> str:
    if not problems and not console_errors and not a11y_blocking_failed:
        n_assets = len(assets)
        n_pages = len(pages)
        base = f"ui_smoke ok: {n_pages} page(s), {n_assets} asset(s) checked, no console errors"
        extra = _quality_warnings(a11y, perf)
        if extra:
            base += " (" + "; ".join(extra) + ")"
        return base
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
    if a11y_blocking_failed and a11y:
        serious = int((a11y.get("counts_by_severity") or {}).get("serious") or 0)
        sample = next((f for f in (a11y.get("findings") or []) if f.get("severity") == "serious"), None)
        detail = f" (e.g. {sample['rule']} on {sample.get('selector')})" if sample else ""
        bits.append(f"{serious} serious a11y finding(s){detail}")
    extra = _quality_warnings(a11y, perf)
    if extra and not a11y_blocking_failed:
        bits.append("; ".join(extra))
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
        a11y: Optional[Dict[str, Any]] = None
        perf: Optional[Dict[str, Any]] = None
        if playwright_available():
            remaining = max(3.0, deadline - time.time())
            try:
                audit = _playwright_audit(base_url, remaining)
                console_errors = audit.get("console_errors") or []
                a11y = audit.get("a11y")
                perf = audit.get("perf")
                playwright_used = True
                # attach to the audited page's own entry too, not just the
                # top-level report — the audit only ever loads base_url + "/".
                for pg in pages:
                    if pg.get("url") == base_url + "/":
                        pg["a11y"] = a11y
                        pg["perf"] = perf
                        break
            except Exception as e:  # noqa: BLE001
                logger.debug("[ui_smoke] playwright pass skipped: %s", e)

        a11y_blocking = playwright_used and a11y is not None and _truthy(
            _setting("ui_smoke_a11y_blocking", False)
        )
        a11y_serious = int(((a11y or {}).get("counts_by_severity") or {}).get("serious") or 0)
        a11y_blocking_failed = bool(a11y_blocking and a11y_serious > 0)

        ok = not problems and not console_errors and not a11y_blocking_failed
        summary = _summarize(pages, assets, console_errors, problems, a11y=a11y, perf=perf,
                              a11y_blocking_failed=a11y_blocking_failed)
        quality_warnings = _quality_warnings(a11y, perf)
        return _report(ran=True, ok=ok, server_cmd=cmd_str, url=base_url, pages=pages,
                        assets=assets, console_errors=console_errors, summary=summary,
                        playwright_used=playwright_used, a11y=a11y, perf=perf,
                        quality_warnings=quality_warnings)
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
    a11y = report.get("a11y")
    if a11y and not report.get("ok"):
        serious = [f for f in (a11y.get("findings") or []) if f.get("severity") == "serious"][:6]
        for f in serious:
            lines.append(f"- a11y ({f.get('rule')}, serious): {f.get('selector')} — {f.get('detail')}")
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
        "quality_warnings": list(report.get("quality_warnings") or []),
    }
    a11y = report.get("a11y")
    if a11y:
        out["a11y_counts"] = a11y.get("counts_by_severity")
    perf = report.get("perf")
    if perf:
        out["perf"] = {
            "lcp_ms": perf.get("lcp_ms"),
            "load_ms": perf.get("load_ms"),
            "js_bytes": perf.get("js_bytes"),
            "warnings": perf.get("warnings") or [],
        }
    return out
