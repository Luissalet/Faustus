"""src/panels.py -- user panels: small self-contained web pages Faustus builds from a
plain-language description and mounts in Studio's sidebar.

Layout, one folder per panel under DATA_DIR/panels/<id>/:

    panel.json            name, icon, description, order, version, dates
    index.html            the current page (self-contained)
    versions/<n>.html     every saved version (n = 1, 2, ...); restoring writes a new one
    storage.json          the panel's own key-value store (faustus.storage)

The page is served on Faustus' own origin (GET /api/panels/{id}/page) inside an iframe.
`faustus-panel.js` (served by routes/panel_routes.py, see PANEL_HELPER_JS) gives it
`faustus.api`, `faustus.tool`, `faustus.theme` and `faustus.storage`.

`check()` loads the page headless with Playwright and reports console errors, page
errors, failed requests and a screenshot, so the agent can test a panel before
handing it over. Nothing here runs the panel's code on the server.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import constants

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_HTML_BYTES = 4_000_000
MAX_STORAGE_BYTES = 2_000_000
MAX_VERSIONS = 200
DEFAULT_ICON = "layout-dashboard"
HELPER_TAG = '<script src="/api/panels/faustus-panel.js"></script>'

_lock = threading.RLock()


class PanelError(Exception):
    def __init__(self, detail: str, error_class: str = "panel.bad_args"):
        super().__init__(detail)
        self.detail = detail
        self.error_class = error_class


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def root() -> Path:
    return Path(constants.DATA_DIR) / "panels"


def _dir(panel_id: str) -> Path:
    if not isinstance(panel_id, str) or not ID_RE.match(panel_id):
        raise PanelError(f"invalid panel id {panel_id!r}", "panel.bad_id")
    return root() / panel_id


def _existing(panel_id: str) -> Path:
    d = _dir(panel_id)
    if not (d / "panel.json").is_file():
        raise PanelError(f"no such panel: {panel_id}", "panel.not_found")
    return d


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", _ascii(name).lower()).strip("-")[:40].strip("-")
    return base or "panel"


def _ascii(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read_meta(d: Path) -> Dict[str, Any]:
    try:
        meta = json.loads((d / "panel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PanelError(f"panel metadata unreadable: {d.name}", "panel.corrupt")
    return meta if isinstance(meta, dict) else {}


def _public(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {k: meta.get(k) for k in ("id", "name", "icon", "description", "order", "version", "created", "updated")}


def _check_html(html: Any) -> str:
    if not isinstance(html, str) or not html.strip():
        raise PanelError("`html` is required: a complete self-contained HTML page")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise PanelError(f"`html` is larger than {MAX_HTML_BYTES // 1_000_000} MB")
    return html


def _clean_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        raise PanelError("`name` is required")
    return text[:60]


def _clean_icon(icon: Any) -> str:
    text = str(icon or "").strip().lower()
    return text[:40] if re.match(r"^[a-z0-9][a-z0-9-]*$", text) else DEFAULT_ICON


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_panels() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    base = root()
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir() and ID_RE.match(d.name) and (d / "panel.json").is_file():
                try:
                    out.append(_public(_read_meta(d)))
                except PanelError:
                    continue
    out.sort(key=lambda m: (m.get("order") if isinstance(m.get("order"), int) else 10_000, str(m.get("created") or ""), str(m.get("id"))))
    return out


def _save_version(d: Path, html: str, version: int) -> None:
    _write(d / "index.html", html)
    _write(d / "versions" / f"{version}.html", html)
    versions = sorted(int(p.stem) for p in (d / "versions").glob("*.html") if p.stem.isdigit())
    for old in versions[:-MAX_VERSIONS]:
        try:
            (d / "versions" / f"{old}.html").unlink()
        except OSError:
            pass


def create(name: Any, html: Any, *, icon: Any = None, description: Any = "", panel_id: Any = None) -> Dict[str, Any]:
    name = _clean_name(name)
    html = _check_html(html)
    with _lock:
        existing = {p["id"] for p in list_panels()}
        if panel_id:
            pid = str(panel_id).strip().lower()
            if not ID_RE.match(pid):
                raise PanelError(f"invalid panel id {panel_id!r}: lowercase letters, digits and hyphens", "panel.bad_id")
            if pid in existing:
                raise PanelError(f"panel {pid} already exists; use panel_update", "panel.exists")
        else:
            pid, n = _slug(name), 2
            while pid in existing:
                pid, n = f"{_slug(name)[:44]}-{n}", n + 1
        d = _dir(pid)
        order = max([p["order"] for p in list_panels() if isinstance(p.get("order"), int)] or [-1]) + 1
        now = _now()
        meta = {"id": pid, "name": name, "icon": _clean_icon(icon), "description": str(description or "")[:400],
                "order": order, "version": 1, "created": now, "updated": now}
        _save_version(d, html, 1)
        _write(d / "panel.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return {**_public(meta), "html_bytes": len(html.encode("utf-8"))}


def get(panel_id: str, *, with_html: bool = True) -> Dict[str, Any]:
    d = _existing(panel_id)
    meta = _public(_read_meta(d))
    if with_html:
        try:
            meta["html"] = (d / "index.html").read_text(encoding="utf-8")
        except OSError:
            meta["html"] = ""
    return meta


def update(panel_id: str, *, html: Any = None, name: Any = None, icon: Any = None, description: Any = None,
           order: Any = None) -> Dict[str, Any]:
    with _lock:
        d = _existing(panel_id)
        meta = _read_meta(d)
        changed_html = False
        if html is not None:
            page = _check_html(html)
            current = ""
            try:
                current = (d / "index.html").read_text(encoding="utf-8")
            except OSError:
                pass
            if page != current:
                meta["version"] = int(meta.get("version") or 0) + 1
                _save_version(d, page, meta["version"])
                changed_html = True
        if name is not None:
            meta["name"] = _clean_name(name)
        if icon is not None:
            meta["icon"] = _clean_icon(icon)
        if description is not None:
            meta["description"] = str(description)[:400]
        if order is not None:
            try:
                meta["order"] = int(order)
            except (TypeError, ValueError):
                raise PanelError("`order` must be an integer")
        meta["updated"] = _now()
        _write(d / "panel.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return {**_public(meta), "html_changed": changed_html}


def reorder(ids: List[str]) -> List[Dict[str, Any]]:
    """Give the listed panels the positions 0..n-1 in that order (others keep their relative order after them)."""
    with _lock:
        known = [p["id"] for p in list_panels()]
        wanted = [i for i in ids if i in known]
        rest = [i for i in known if i not in wanted]
        for pos, pid in enumerate(wanted + rest):
            update(pid, order=pos)
        return list_panels()


def delete(panel_id: str) -> Dict[str, Any]:
    with _lock:
        d = _existing(panel_id)
        shutil.rmtree(d, ignore_errors=True)
        return {"deleted": panel_id}


def versions(panel_id: str) -> Dict[str, Any]:
    d = _existing(panel_id)
    meta = _read_meta(d)
    rows = []
    for p in (d / "versions").glob("*.html"):
        if p.stem.isdigit():
            st = p.stat()
            rows.append({"version": int(p.stem), "bytes": st.st_size,
                         "saved": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    rows.sort(key=lambda r: -r["version"])
    return {"id": panel_id, "current": meta.get("version"), "versions": rows}


def version_html(panel_id: str, version: int) -> str:
    d = _existing(panel_id)
    p = d / "versions" / f"{int(version)}.html"
    if not p.is_file():
        raise PanelError(f"panel {panel_id} has no version {version}", "panel.not_found")
    return p.read_text(encoding="utf-8")


def restore(panel_id: str, version: Any) -> Dict[str, Any]:
    try:
        n = int(version)
    except (TypeError, ValueError):
        raise PanelError("`version` must be an integer")
    html = version_html(panel_id, n)
    out = update(panel_id, html=html)
    out["restored_from"] = n
    return out


def page_html(panel_id: str) -> str:
    """The page as served: the stored HTML with the helper script added when it is missing."""
    html = get(panel_id)["html"]
    if "faustus-panel.js" in html:
        return html
    m = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + HELPER_TAG + html[m.end():]
    m = re.search(r"<html[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + "<head>" + HELPER_TAG + "</head>" + html[m.end():]
    return HELPER_TAG + html


# ---------------------------------------------------------------------------
# per-panel storage (faustus.storage)
# ---------------------------------------------------------------------------
def _storage_path(panel_id: str) -> Path:
    return _existing(panel_id) / "storage.json"


def _storage_load(panel_id: str) -> Dict[str, Any]:
    p = _storage_path(panel_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def storage_list(panel_id: str) -> Dict[str, Any]:
    return _storage_load(panel_id)


def storage_get(panel_id: str, key: str) -> Any:
    if not KEY_RE.match(key or ""):
        raise PanelError("invalid storage key")
    return _storage_load(panel_id).get(key)


def storage_set(panel_id: str, key: str, value: Any) -> Dict[str, Any]:
    if not KEY_RE.match(key or ""):
        raise PanelError("invalid storage key: up to 128 letters, digits and _ . : -")
    with _lock:
        data = _storage_load(panel_id)
        data[key] = value
        text = json.dumps(data, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_STORAGE_BYTES:
            raise PanelError(f"panel storage is limited to {MAX_STORAGE_BYTES // 1_000_000} MB", "panel.too_large")
        _write(_storage_path(panel_id), text)
    return {"key": key, "stored": True}


def storage_delete(panel_id: str, key: str) -> Dict[str, Any]:
    with _lock:
        data = _storage_load(panel_id)
        existed = data.pop(key, None) is not None
        _write(_storage_path(panel_id), json.dumps(data, ensure_ascii=False))
    return {"key": key, "deleted": existed}


# ---------------------------------------------------------------------------
# check: render headless and report what went wrong
# ---------------------------------------------------------------------------
def check(panel_id: str, *, base_url: Optional[str] = None, wait_ms: int = 1500, width: int = 1100,
          height: int = 760, screenshot: bool = True, token: Optional[str] = None) -> Dict[str, Any]:
    """Open the panel's served page in headless Chromium and report console errors, page
    errors and failed requests. The screenshot is written under the panel folder
    (`check.png`) and its path returned."""
    d = _existing(panel_id)
    base = (base_url or constants.internal_api_base()).rstrip("/")
    url = f"{base}/api/panels/{panel_id}/page"
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise PanelError(f"Playwright is not installed: {exc}", "panel.no_browser")
    console: List[Dict[str, str]] = []
    page_errors: List[str] = []
    failed: List[Dict[str, Any]] = []
    status: Optional[int] = None
    title = ""
    text_len = 0
    shot_path = None
    headers = {}
    if token:
        from core.middleware import INTERNAL_TOOL_HEADER
        headers[INTERNAL_TOOL_HEADER] = token
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height}, service_workers="block",
                                      extra_http_headers=headers or None)
            page = ctx.new_page()
            page.set_default_timeout(15000)
            page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:400]}) if len(console) < 100 else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)[:400]) if len(page_errors) < 50 else None)
            page.on("requestfailed", lambda r: failed.append({"url": r.url[:200], "error": (r.failure or "")[:120]})
                    if len(failed) < 50 else None)

            def on_response(r):
                if r.status >= 400 and len(failed) < 50:
                    failed.append({"url": r.url[:200], "status": r.status})
            page.on("response", on_response)
            try:
                resp = page.goto(url, wait_until="load", timeout=20000)
                status = resp.status if resp else None
            except Exception as exc:  # noqa: BLE001
                raise PanelError(f"could not load {url}: {str(exc).splitlines()[0][:200]}", "panel.load_failed")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(max(0, min(int(wait_ms), 10000)))
            title = page.title()
            try:
                text_len = len((page.inner_text("body") or "").strip())
            except Exception:  # noqa: BLE001
                text_len = 0
            if screenshot:
                shot_path = d / "check.png"
                page.screenshot(path=str(shot_path), full_page=False)
        finally:
            browser.close()
    errors = [c for c in console if c["type"] == "error"]
    # Drop the browser's own echo of a failed request: it is already listed under `failed_requests`.
    errors = [c for c in errors if not c["text"].startswith("Failed to load resource")]
    ok = status == 200 and not errors and not page_errors and not failed and text_len > 0
    notes = []
    if text_len == 0:
        notes.append("the page rendered no visible text")
    return {"id": panel_id, "url": url, "ok": ok, "status": status, "title": title, "visible_text_chars": text_len,
            "console_errors": errors, "console_other": [c for c in console if c["type"] in ("warning", "warn")][:20],
            "page_errors": page_errors, "failed_requests": failed, "notes": notes,
            "screenshot": str(shot_path) if shot_path else None}


# ---------------------------------------------------------------------------
# the helper script served as /api/panels/faustus-panel.js
# ---------------------------------------------------------------------------
PANEL_HELPER_JS = r"""/* faustus-panel.js -- helper for Faustus panels.
 *   faustus.id                       this panel's id
 *   faustus.api(path, opts)          fetch a Faustus /api/* path (JSON in, JSON out; opts.body objects are sent as JSON)
 *   faustus.tool(name, args, app)    run an agent tool, or a tool of a connected Hoard when `app` is given
 *   faustus.theme                    {vars:{--fs-*:value}, mode:'dark'|'light'}; also set on :root as CSS variables
 *   faustus.storage                  get(key), set(key, value), remove(key), all()  -- saved on the server per panel
 *   faustus.onTheme(fn)              called when the theme changes
 */
(function () {
  var m = location.pathname.match(/\/api\/panels\/([^/]+)\/page/);
  var id = m ? m[1] : (document.currentScript && document.currentScript.dataset.panel) || '';
  var fallback = {
    '--fs-bg': '#0f1115', '--fs-surface-1': '#171a21', '--fs-surface-2': '#1f232c', '--fs-border': '#2b303b',
    '--fs-text-1': '#e8eaf0', '--fs-text-2': '#b3b9c6', '--fs-text-3': '#8a91a0', '--fs-brand': '#ff7a59'
  };
  var listeners = [];
  function readTheme() {
    var vars = {}, mode = 'dark';
    try {
      var root = window.parent && window.parent !== window ? window.parent.document.documentElement : null;
      if (root) {
        var cs = window.parent.getComputedStyle(root);
        Object.keys(fallback).forEach(function (k) { var v = cs.getPropertyValue(k).trim(); if (v) vars[k] = v; });
        var sheets = window.parent.document.styleSheets;
        for (var i = 0; i < sheets.length; i++) {
          var rules; try { rules = sheets[i].cssRules; } catch (e) { continue; }
          for (var j = 0; j < rules.length; j++) {
            var st = rules[j].style;
            if (!st) continue;
            for (var p = 0; p < st.length; p++) {
              var name = st[p];
              if (name.indexOf('--fs-') === 0 && !(name in vars)) { var v2 = cs.getPropertyValue(name).trim(); if (v2) vars[name] = v2; }
            }
          }
        }
        var attr = root.getAttribute('data-theme');
        mode = attr === 'light' || attr === 'dark' ? attr : (window.parent.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      }
    } catch (e) { /* cross-origin or no parent: use the fallback */ }
    Object.keys(fallback).forEach(function (k) { if (!(k in vars)) vars[k] = fallback[k]; });
    return { vars: vars, mode: mode };
  }
  function applyTheme() {
    var th = readTheme();
    api.theme = th;
    var st = document.documentElement.style;
    Object.keys(th.vars).forEach(function (k) { st.setProperty(k, th.vars[k]); });
    document.documentElement.setAttribute('data-theme', th.mode);
    document.documentElement.style.colorScheme = th.mode;
    listeners.forEach(function (fn) { try { fn(th); } catch (e) { console.error(e); } });
  }
  function req(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'), headers: Object.assign({}, opts.headers || {}) };
    if (opts.body !== undefined) {
      if (typeof opts.body === 'string' || opts.body instanceof FormData || opts.body instanceof Blob) init.body = opts.body;
      else { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
    }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (txt) {
        var data; try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = txt; }
        if (!r.ok) { var err = new Error((data && (data.error || data.detail)) || ('HTTP ' + r.status)); err.status = r.status; err.data = data; throw err; }
        return data;
      });
    });
  }
  function api(path, opts) { return req(path, opts); }
  api.api = api;
  api.id = id;
  api.tool = function (name, args, app) {
    return req('/api/panels/tool', { body: { tool: name, args: args || {}, app: app || undefined } });
  };
  var base = '/api/panels/' + encodeURIComponent(id) + '/storage';
  api.storage = {
    get: function (key, dflt) { return req(base + '/' + encodeURIComponent(key)).then(function (r) { return r && r.value !== null && r.value !== undefined ? r.value : dflt; }); },
    set: function (key, value) { return req(base + '/' + encodeURIComponent(key), { method: 'PUT', body: { value: value } }); },
    remove: function (key) { return req(base + '/' + encodeURIComponent(key), { method: 'DELETE' }); },
    all: function () { return req(base); }
  };
  api.onTheme = function (fn) { listeners.push(fn); };
  api.theme = { vars: fallback, mode: 'dark' };
  window.faustus = api;
  applyTheme();
  try {
    var obs = new MutationObserver(applyTheme);
    if (window.parent && window.parent !== window) obs.observe(window.parent.document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style', 'class'] });
  } catch (e) { /* ignore */ }
})();
"""
