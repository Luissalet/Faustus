"""Watch a web page do one thing, in a fresh headless browser, and keep the evidence.

``observe`` opens the URL in a clean Chromium profile (no logins, no service
workers, no downloads) and records, through the DevTools protocol:

* every request with its initiator -- the script, line and column, or the
  call stack, that issued it -- plus status and type;
* the scripts the page parsed and the source map each one declares;
* precise JS coverage in two slices: what ran while loading, and what ran
  during the user's actions only (``Profiler.takePreciseCoverage`` resets the
  counters, so the second slice is exactly the action);
* DOM mutations during the actions (MutationObserver), console messages and
  uncaught errors, screenshots before and after.

Generated positions are mapped back to original modules with the published
source maps (``sourcemap.py``). Nothing here interprets the result: the
evidence is assembled by ``web_explain.explain_feature``.
"""

from __future__ import annotations

import bisect
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .sourcemap import SourceMap, data_url_text, load_map_text, package_of

MAX_MAP_BYTES = 40_000_000
MAX_MAPS = 12
_LINE_BREAK = re.compile("(\r\n|[\n\r\u2028\u2029])")
SECRET = re.compile(r"pass(?:word)?|token|secret|auth|session|cookie|api[_-]?key|credential|otp|pin\b", re.I)
API_TYPES = {"XHR", "Fetch", "EventSource", "WebSocket", "Document", "Other", "Ping"}
# Interstitials of bot checks. They are reported, never worked around.
BOT_CHECK = re.compile(r"just a moment|attention required|verify you are (?:a )?human|are you a robot|captcha|access denied|"
                       r"checking your browser|ddos protection", re.I)


class WebExplainError(Exception):
    """Bad input or a page that could not be observed; the message is for the user."""


def check_url(url: str) -> str:
    u = (url or "").strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise WebExplainError(f"need an http(s) URL, got {url!r}")
    return u


def redact(text: str, limit: int = 600) -> str:
    """Request bodies may carry secrets: values of secret-looking keys are masked, the rest truncated."""
    if not text:
        return ""
    t = re.sub(r'("?[\w.-]*(?:pass(?:word)?|token|secret|auth|session|api[_-]?key|credential|otp)[\w.-]*"?\s*[:=]\s*)'
               r'("[^"]*"|[^&,\s}]+)', r"\1***", text, flags=re.I)
    return t[:limit] + ("..." if len(t) > limit else "")


FINGERPRINT_JS = r"""() => {
  const out = {libs: [], meta: {}};
  const add = (name, version, how) => out.libs.push({name, version: version || null, how});
  const w = window;
  const nodes = Array.from(document.querySelectorAll('body, body *')).slice(0, 1500);
  const keyed = (pfx) => nodes.some(n => Object.keys(n).some(k => k.startsWith(pfx)));
  if (keyed('__reactFiber$') || keyed('__reactContainer$') || keyed('_reactRootContainer') || document.querySelector('[data-reactroot]'))
    add('React', (w.React && w.React.version) || null, 'React fiber keys on DOM nodes');
  if (w.__NEXT_DATA__) add('Next.js', null, 'window.__NEXT_DATA__ (page ' + (w.__NEXT_DATA__.page || '?') + ', build ' + (w.__NEXT_DATA__.buildId || '?') + ')');
  if (document.querySelector('script[src*="/_next/"]') && !w.__NEXT_DATA__) add('Next.js', null, '/_next/ script chunks (app router)');
  const vueRoot = nodes.find(n => n.__vue_app__);
  if (vueRoot) add('Vue', vueRoot.__vue_app__.version, '__vue_app__ on the root element');
  else if (keyed('__vue__') || keyed('__vueParentComponent')) add('Vue', (w.Vue && w.Vue.version) || null, 'Vue component keys on DOM nodes');
  if (w.__NUXT__ || w.useNuxtApp) add('Nuxt', null, 'window.__NUXT__');
  const ng = document.querySelector('[ng-version]');
  if (ng) add('Angular', ng.getAttribute('ng-version'), 'ng-version attribute');
  if (nodes.some(n => typeof n.className === 'string' && /\bsvelte-[a-z0-9]+\b/.test(n.className))) add('Svelte', null, 'svelte-* scoped classes');
  if (w.jQuery && w.jQuery.fn) add('jQuery', w.jQuery.fn.jquery, 'window.jQuery');
  if (w.__remixContext) add('Remix', null, 'window.__remixContext');
  if (w.__APOLLO_STATE__ || w.__APOLLO_CLIENT__) add('Apollo GraphQL', null, 'Apollo state on window');
  if (w.Alpine) add('Alpine.js', w.Alpine.version, 'window.Alpine');
  if (w.htmx) add('htmx', w.htmx.version, 'window.htmx');
  if (w.Shopify) add('Shopify', null, 'window.Shopify');
  if (w.wp || document.querySelector('link[href*="wp-content"]')) add('WordPress', null, 'wp-content assets');
  if (document.querySelector('astro-island')) add('Astro', null, 'astro-island elements');
  if (w.__sveltekit_dev || document.querySelector('[data-sveltekit-preload-data]')) add('SvelteKit', null, 'data-sveltekit attributes');
  if (w.Ember) add('Ember', w.Ember.VERSION, 'window.Ember');
  if (w.Zone && w.ng) add('Angular (dev)', null, 'window.ng');
  const custom = Array.from(new Set(nodes.map(n => n.localName).filter(t => t.includes('-') && customElements.get(t)))).slice(0, 15);
  if (custom.length) out.meta.custom_elements = custom;
  const gen = document.querySelector('meta[name="generator"]');
  if (gen) out.meta.generator = gen.content;
  out.meta.elements = nodes.length;
  out.meta.scripts = document.scripts.length;
  out.meta.lang = document.documentElement.lang || null;
  return out;
}"""

MUTATION_INSTALL_JS = r"""() => {
  const d = (n) => {
    if (!n || n.nodeType !== 1) return n && n.nodeType === 3 ? '#text "' + (n.textContent || '').trim().slice(0, 60) + '"' : null;
    let s = n.localName;
    if (n.id) s += '#' + n.id;
    const cls = (typeof n.className === 'string' ? n.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    const t = (n.innerText || n.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    return t ? s + ' "' + t + '"' : s;
  };
  const r = window.__wx = {added: 0, removed: 0, attrs: 0, text: 0, samples: []};
  const keep = (x) => { if (x && r.samples.length < 40) r.samples.push(x); };
  window.__wxObs = new MutationObserver((list) => {
    for (const m of list) {
      if (m.type === 'childList') {
        m.addedNodes.forEach(n => { r.added++; const x = d(n); if (x && x !== '#text ""') keep('+ ' + x); });
        m.removedNodes.forEach(n => { r.removed++; const x = d(n); if (x && x !== '#text ""') keep('- ' + x); });
      } else if (m.type === 'attributes') {
        r.attrs++; keep('~ ' + d(m.target) + ' [' + m.attributeName + ']');
      } else { r.text++; keep('~ text in ' + d(m.target.parentElement)); }
    }
  });
  window.__wxObs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
}"""

MUTATION_COLLECT_JS = "() => { const r = window.__wx || null; if (window.__wxObs) window.__wxObs.disconnect(); return r; }"


def _data_dir() -> Path:
    import os

    override = os.environ.get("FAUSTUS_WEB_EXPLAIN_DIR")
    if override:
        return Path(override)
    try:
        from src.constants import DATA_DIR

        return Path(DATA_DIR) / "web_explain"
    except Exception:  # pragma: no cover - standalone use
        return Path.cwd() / "data" / "web_explain"


def _locate(page, target: str):
    if not target:
        raise WebExplainError("the action needs a target (CSS selector or visible text)")
    if target.startswith(("text=", "role=", "css=", "xpath=", "//", "internal:")):
        return page.locator(target).first
    try:
        loc = page.locator(target)
        if loc.count() > 0:
            return loc.first
    except Exception:
        pass
    for cand in (page.get_by_role("button", name=target), page.get_by_role("link", name=target),
                 page.get_by_label(target), page.get_by_placeholder(target), page.get_by_text(target)):
        try:
            if cand.count() > 0:
                return cand.first
        except Exception:
            continue
    raise WebExplainError(f"nothing on the page matches {target!r}")


def _perform(page, action: dict) -> str:
    kind = str(action.get("type") or "click").lower()
    target = str(action.get("target") or "")
    value = action.get("value")
    if kind == "wait":
        page.wait_for_timeout(min(int(value or 1000), 10000))
        return f"wait {value or 1000} ms"
    if kind == "scroll":
        page.mouse.wheel(0, int(value or 800))
        return f"scroll {value or 800} px"
    if kind == "press" and not target:
        page.keyboard.press(str(value or "Enter"))
        return f"press {value or 'Enter'}"
    if kind == "goto":
        page.goto(check_url(target))
        return f"goto {target}"
    loc = _locate(page, target)
    if kind == "click":
        loc.click(timeout=8000)
    elif kind in ("fill", "type"):
        loc.fill(str(value or ""), timeout=8000)
    elif kind == "hover":
        loc.hover(timeout=8000)
    elif kind == "press":
        loc.press(str(value or "Enter"), timeout=8000)
    elif kind == "select":
        loc.select_option(str(value or ""), timeout=8000)
    else:
        raise WebExplainError(f"unknown action type {kind!r} (click, fill, hover, press, select, scroll, wait, goto)")
    shown = "***" if kind in ("fill", "type") and SECRET.search(target) else value
    return f"{kind} {target!r}" + (f" = {shown!r}" if value is not None and kind != "click" else "")


class _Script:
    __slots__ = ("id", "url", "start_line", "start_col", "map_url", "length", "source", "_starts", "map", "map_error")

    def __init__(self, p: dict):
        self.id = p.get("scriptId")
        self.url = p.get("url") or ""
        self.start_line = int(p.get("startLine") or 0)
        self.start_col = int(p.get("startColumn") or 0)
        ref = p.get("sourceMapURL") or ""
        self.map_url = ref if not ref or ref.startswith("data:") else urljoin(self.url, ref)
        self.length = int(p.get("length") or 0)
        self.source: str | None = None
        self._starts: list[int] | None = None
        self.map: SourceMap | None = None
        self.map_error: str | None = None

    def position(self, offset: int) -> tuple[int, int] | None:
        """0-based (line, column) inside the script text for an offset in UTF-16 code units (what V8 coverage and
        source map columns count), so characters outside the BMP before or on a line do not shift the result."""
        if self.source is None:
            return None
        if self._starts is None:
            starts, pos = [0], 0
            for k, part in enumerate(_LINE_BREAK.split(self.source)):
                pos += len(part.encode("utf-16-le")) // 2
                if k % 2:
                    starts.append(pos)
            self._starts = starts
        i = bisect.bisect_right(self._starts, offset) - 1
        return i, offset - self._starts[i]


def _origin(script: _Script | None, line0: int, col0: int) -> dict | None:
    """Original position (via the source map) of a 0-based position inside a script."""
    if script is None or script.map is None:
        return None
    hit = script.map.lookup(line0 + 1, col0)
    if not hit:
        return None
    pkg = package_of(hit["source"])
    return {"source": hit["source"], "line": hit["line"], "name": hit["name"],
            "package": pkg[0] if pkg else None}


def _short(url: str, limit: int = 160) -> str:
    return url if len(url) <= limit else url[: limit - 3] + "..."


def observe(url: str, actions: list[dict] | None = None, wait_ms: int = 1500, timeout_s: int = 40,
            out_dir: str | Path | None = None, headless: bool = True) -> tuple[dict, dict]:
    """Load ``url``, run ``actions``, and return ``(report, internals)``; internals keep the parsed maps."""
    url = check_url(url)
    actions = [a if isinstance(a, dict) else {"type": "click", "target": str(a)} for a in (actions or [])]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise WebExplainError("Playwright is not installed: pip install playwright && playwright install chromium") from exc

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    out = Path(out_dir or _data_dir()) / run_id
    out.mkdir(parents=True, exist_ok=True)
    phase = {"now": "load"}
    reqs: dict[str, dict] = {}
    scripts: dict[str, _Script] = {}
    console: list[dict] = []
    errors: list[str] = []
    report: dict = {"url": url, "run_dir": str(out), "notes": []}
    cov_action: list[dict] = []
    mutations = None
    action_log: list[str] = []

    def on_request(p):
        r = p.get("request") or {}
        rid = p.get("requestId")
        prev = reqs.get(rid)
        if prev and p.get("redirectResponse"):
            prev["status"] = p["redirectResponse"].get("status")
            prev["done"] = True
            reqs[f"{rid}#{len(reqs)}"] = prev  # the redirect hop keeps its row; the id now follows the new request
        reqs[rid] = {"cdp_id": p.get("requestId"), "url": r.get("url", ""), "method": r.get("method", "GET"),
                     "type": p.get("type") or "Other", "phase": phase["now"], "initiator": p.get("initiator") or {},
                     "post": r.get("postData") or "", "status": None, "mime": None, "failed": None, "done": False,
                     "t": p.get("timestamp")}

    def on_response(p):
        rec = reqs.get(p.get("requestId"))
        if rec:
            resp = p.get("response") or {}
            rec["status"], rec["mime"] = resp.get("status"), resp.get("mimeType")
            if p.get("type"):
                rec["type"] = p["type"]

    def on_finished(p):
        rec = reqs.get(p.get("requestId"))
        if rec:
            rec["done"] = True

    def on_failed(p):
        rec = reqs.get(p.get("requestId"))
        if rec:
            rec["failed"] = p.get("errorText") or "failed"

    def on_ws(p):
        reqs[p.get("requestId")] = {"cdp_id": p.get("requestId"), "url": p.get("url", ""), "method": "WS",
                                    "type": "WebSocket", "phase": phase["now"], "initiator": p.get("initiator") or {},
                                    "post": "", "status": 101, "mime": None, "failed": None, "done": False, "t": None}

    def on_script(p):
        s = _Script(p)
        if s.id:
            scripts[s.id] = s

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 800}, accept_downloads=False,
                                          service_workers="block", locale="en-US")
            page = context.new_page()
            page.set_default_timeout(timeout_s * 1000)
            cdp = context.new_cdp_session(page)
            cdp.on("Network.requestWillBeSent", on_request)
            cdp.on("Network.responseReceived", on_response)
            cdp.on("Network.loadingFinished", on_finished)
            cdp.on("Network.loadingFailed", on_failed)
            cdp.on("Network.webSocketCreated", on_ws)
            cdp.on("Debugger.scriptParsed", on_script)
            cdp.send("Network.enable", {"maxTotalBufferSize": 50_000_000, "maxResourceBufferSize": 5_000_000})
            cdp.send("Debugger.enable")
            cdp.send("Profiler.enable")
            cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": False})
            page.on("console", lambda m: console.append({"phase": phase["now"], "type": m.type, "text": m.text[:300]})
                    if len(console) < 200 else None)
            page.on("pageerror", lambda e: errors.append(f"[{phase['now']}] {str(e)[:300]}") if len(errors) < 50 else None)

            try:
                resp = page.goto(url, wait_until="load", timeout=timeout_s * 1000)
            except Exception as exc:
                raise WebExplainError(f"could not load {url}: {str(exc).splitlines()[0][:200]}") from exc
            report["http_status"] = resp.status if resp else None
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                report["notes"].append("the page kept the network busy; observed after 8 s")
            page.wait_for_timeout(600)
            report["final_url"], report["title"] = page.url, page.title()
            if BOT_CHECK.search(report["title"] or "") or (report["http_status"] in (403, 429, 503)
                                                           and len(page.content()) < 60_000):
                report["blocked"] = (f"the site answered HTTP {report['http_status']} with a bot check or block page "
                                     f"({report['title']!r}); it is not bypassed")
                report["notes"].append(report["blocked"])
            try:
                report["fingerprint"] = page.evaluate(FINGERPRINT_JS)
            except Exception as exc:
                report["fingerprint"] = {"libs": [], "meta": {}, "error": str(exc)[:200]}
            page.screenshot(path=str(out / "before.png"))
            report["screenshots"] = {"before": str(out / "before.png")}
            cdp.send("Profiler.takePreciseCoverage")  # discard the load slice; counters reset

            if actions:
                try:
                    page.evaluate(MUTATION_INSTALL_JS)
                except Exception:
                    pass
                phase["now"] = "action"
                before_url = page.url
                for a in actions:
                    try:
                        action_log.append(_perform(page, a))
                    except Exception as exc:  # a missing target is reported with what was observed so far
                        action_log.append(f"FAILED {a}: {str(exc).splitlines()[0][:200]}")
                        break
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                page.wait_for_timeout(max(0, min(int(wait_ms), 15000)))
                cov_action = cdp.send("Profiler.takePreciseCoverage").get("result") or []
                try:
                    mutations = page.evaluate(MUTATION_COLLECT_JS)
                except Exception:
                    mutations = None
                if page.url != before_url:
                    report["navigated_to"] = page.url
                page.screenshot(path=str(out / "after.png"))
                report["screenshots"]["after"] = str(out / "after.png")

            # Response previews of what the actions fetched (API calls only).
            for rec in reqs.values():
                if rec["phase"] == "action" and rec["type"] in ("XHR", "Fetch") and rec["done"]:
                    try:
                        body = cdp.send("Network.getResponseBody", {"requestId": rec["cdp_id"]})
                        if not body.get("base64Encoded"):
                            rec["response"] = redact(body.get("body") or "", 700)
                    except Exception:
                        pass

            # Script sources for the positions we must translate, and their source maps.
            wanted: list[str] = []
            for fn in cov_action:
                if any(f["ranges"][0]["count"] > 0 for f in fn.get("functions", []) if f.get("ranges")):
                    wanted.append(fn.get("scriptId"))
            for rec in reqs.values():
                for frame in _frames(rec["initiator"])[:1]:
                    wanted.append(frame.get("scriptId"))
            seen_maps: dict[str, SourceMap | str] = {}
            for sid in dict.fromkeys(w for w in wanted if w in scripts):
                s = scripts[sid]
                try:
                    s.source = cdp.send("Debugger.getScriptSource", {"scriptId": sid}).get("scriptSource")
                except Exception:
                    s.source = None
                if s.map_url and (s.map_url in seen_maps or len(seen_maps) < MAX_MAPS):
                    if s.map_url not in seen_maps:
                        seen_maps[s.map_url] = _fetch_map(context, s.map_url, s.url)
                    got = seen_maps[s.map_url]
                    if isinstance(got, SourceMap):
                        s.map = got
                    else:
                        s.map_error = got
            # Maps of the remaining page scripts (for inventory and search), within the budget.
            for s in scripts.values():
                if s.map is None and s.map_error is None and s.map_url and len(seen_maps) < MAX_MAPS:
                    if s.map_url not in seen_maps:
                        seen_maps[s.map_url] = _fetch_map(context, s.map_url, s.url)
                    got = seen_maps[s.map_url]
                    if isinstance(got, SourceMap):
                        s.map = got
                    else:
                        s.map_error = got
        finally:
            browser.close()

    report["actions"] = action_log
    report["requests"] = _requests(reqs, scripts)
    report["mutations"] = mutations
    report["ran"] = _ran(cov_action, scripts) if actions else None
    report["console"] = [c for c in console if c["type"] in ("error", "warning")][:20] + \
        [c for c in console if c["phase"] == "action" and c["type"] not in ("error", "warning")][:10]
    report["errors"] = errors[:20]
    report["scripts"] = _script_inventory(scripts)
    maps = {u: m for u, m in seen_maps.items() if isinstance(m, SourceMap)}
    return report, {"maps": maps, "scripts": scripts}


def _frames(initiator: dict) -> list[dict]:
    """Call frames of an initiator, innermost first: the stack when there is one, else the parser position."""
    frames: list[dict] = []
    stack = initiator.get("stack")
    while stack:
        frames.extend(stack.get("callFrames") or [])
        stack = stack.get("parent")
    if not frames and initiator.get("url"):
        frames.append({"url": initiator["url"], "lineNumber": initiator.get("lineNumber", 0),
                       "columnNumber": initiator.get("columnNumber", 0), "functionName": "", "scriptId": None})
    return frames


def _fetch_map(context, map_url: str, script_url: str) -> SourceMap | str:
    try:
        inline = data_url_text(map_url)
        if inline is not None:
            return SourceMap(load_map_text(inline), "inline")
        resp = context.request.get(map_url, timeout=20000, max_redirects=3)
        if resp.status != 200:
            return f"HTTP {resp.status}"
        body = resp.body()
        if len(body) > MAX_MAP_BYTES:
            return f"map larger than {MAX_MAP_BYTES // 1_000_000} MB"
        return SourceMap(load_map_text(body.decode("utf-8", "replace")), map_url)
    except Exception as exc:
        return f"unreadable map: {str(exc).splitlines()[0][:120]}"


def _locate_frame(frame: dict, scripts: dict[str, _Script]) -> dict:
    s = scripts.get(frame.get("scriptId")) if frame.get("scriptId") else None
    line0, col0 = int(frame.get("lineNumber") or 0), int(frame.get("columnNumber") or 0)
    if s is not None:
        line0, col0 = line0 - s.start_line if line0 >= s.start_line else line0, col0
    out = {"script": _short(frame.get("url") or (s.url if s else "")), "line": int(frame.get("lineNumber") or 0) + 1,
           "column": col0, "function": frame.get("functionName") or None}
    orig = _origin(s, line0, col0) if s is not None else None
    if orig:
        out["original"] = orig
    return out


def _requests(reqs: dict[str, dict], scripts: dict[str, _Script]) -> dict:
    rows_action, load_api, by_type = [], [], {}
    n = 0
    for rec in sorted(reqs.values(), key=lambda r: r.get("t") or 0):
        by_type[rec["type"]] = by_type.get(rec["type"], 0) + 1
        if rec["url"].startswith("data:"):
            continue
        row = {"method": rec["method"], "url": _short(rec["url"]), "type": rec["type"], "status": rec["status"],
               "mime": rec["mime"]}
        if rec["failed"]:
            row["failed"] = rec["failed"]
        if rec["phase"] == "action":
            n += 1
            row["id"] = f"R{n}"
            frames = _frames(rec["initiator"])
            if frames:
                row["initiator"] = _locate_frame(frames[0], scripts)
                own = next((f for f in frames if _origin_first_party(f, scripts)), None)
                if own is not None and own is not frames[0]:
                    row["first_party_caller"] = _locate_frame(own, scripts)
            elif rec["initiator"].get("type"):
                row["initiator"] = {"type": rec["initiator"]["type"]}
            if rec["post"]:
                row["body"] = redact(rec["post"])
            if rec.get("response"):
                row["response"] = rec["response"]
            if len(rows_action) < 40:
                rows_action.append(row)
        elif rec["type"] in ("XHR", "Fetch", "WebSocket", "EventSource") and len(load_api) < 25:
            load_api.append(row)
    return {"during_actions": rows_action, "load_api_calls": load_api, "by_type": by_type, "total": len(reqs)}


def _origin_first_party(frame: dict, scripts: dict[str, _Script]) -> bool:
    s = scripts.get(frame.get("scriptId")) if frame.get("scriptId") else None
    if s is None or s.map is None:
        return False
    orig = _origin(s, int(frame.get("lineNumber") or 0) - s.start_line, int(frame.get("columnNumber") or 0))
    return bool(orig and not orig["package"])


def _ran(coverage: list[dict], scripts: dict[str, _Script]) -> dict:
    """Functions that executed during the actions, mapped to original modules, first-party first."""
    rows: list[dict] = []
    packages: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    total = 0
    for entry in coverage:
        s = scripts.get(entry.get("scriptId"))
        if not (entry.get("url") or (s.url if s else "")):
            continue  # eval'd or injected by the automation itself (no URL): not the site's code
        for fn in entry.get("functions") or []:
            rng = (fn.get("ranges") or [{}])[0]
            count = int(rng.get("count") or 0)
            if count <= 0:
                continue
            name = fn.get("functionName") or ""
            start = int(rng.get("startOffset") or 0)
            if not name and start == 0:
                continue  # the script body itself
            total += 1
            url = entry.get("url") or (s.url if s else "")
            pos = s.position(start) if s is not None else None
            orig = _origin(s, pos[0], pos[1]) if pos else None
            if orig and orig["package"]:
                packages[orig["package"]] = packages.get(orig["package"], 0) + 1
                continue
            if orig is None:
                key = _short(url, 120) or "(inline/eval)"
                unmapped[key] = unmapped.get(key, 0) + 1
                if s is not None and s.map is not None:
                    continue
            row = {"function": (orig or {}).get("name") or name or "(anonymous)", "calls": count,
                   "generated": {"script": _short(url, 120),
                                 "line": (pos[0] + 1 + (s.start_line if s else 0)) if pos else None,
                                 "column": pos[1] if pos else None}}
            if orig:
                row["original"] = {"source": orig["source"], "line": orig["line"]}
            rows.append(row)
    rows.sort(key=lambda r: ("original" not in r, -r["calls"]))
    pk = []
    for name, cnt in sorted(packages.items(), key=lambda kv: -kv[1]):
        pk.append({"name": name, "functions": cnt})
    return {"functions_total": total, "first_party": rows[:60], "packages": pk[:25],
            "unmapped_scripts": [{"script": k, "functions": v} for k, v in sorted(unmapped.items(), key=lambda kv: -kv[1])][:15]}


def _script_inventory(scripts: dict[str, _Script]) -> dict:
    with_map, without, inline = [], 0, 0
    versions: dict[str, str | None] = {}
    for s in scripts.values():
        if not s.url:
            inline += 1
            continue
        if s.map is not None:
            if all(w["map"] != s.map.map_url for w in with_map):
                with_map.append({"script": _short(s.url, 120), "map": s.map.map_url[:160] if s.map.map_url != "inline" else "inline",
                                 "sources": len(s.map.sources), "first_party": len(s.map.first_party())})
                for p in s.map.packages():
                    if p["name"] not in versions or (p["version"] and not versions[p["name"]]):
                        versions[p["name"]] = p["version"]
        else:
            without += 1
    return {"with_source_map": with_map[:15], "without_source_map": without, "inline_or_eval": inline,
            "packages": [{"name": k, "version": v} for k, v in sorted(versions.items())][:80]}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
