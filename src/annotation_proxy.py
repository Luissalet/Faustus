"""src/annotation_proxy.py -- a local web page, live, inside Studio's Annotate tab.

A page served on this machine (``http://localhost:5173/`` from a dev server, a Hoard, any loopback port) is
shown through a small proxy of its own on ``127.0.0.1:<free port>``: one per upstream origin, so the page keeps
absolute paths, its module graph and its hot-reload socket (WebSocket upgrades are piped through untouched).
HTML documents get a short script before ``</head>`` that lets the person select text or pick an element and
posts it to the Studio window (``faustus-annotate/*`` messages); a full reload injects it again.

The proxies run on one background thread with their own event loop, so they outlive any request and do not
depend on the web server's loop.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_HEAD_LIMIT = 64 * 1024
_HTML_LIMIT = 8 * 1024 * 1024

_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_proxies: Dict[str, Tuple[asyncio.AbstractServer, int]] = {}   # upstream origin -> (server, local port)


class ProxyError(ValueError):
    pass


def check_upstream(url: str) -> str:
    """A loopback http(s) URL. ``5173`` and ``:5173`` mean ``http://localhost:5173/``."""
    raw = str(url or "").strip()
    if re.fullmatch(r":?\d{2,5}", raw):
        raw = "http://localhost:" + raw.lstrip(":") + "/"
    elif raw and "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in _LOOPBACK or not parts.port:
        from src.annotations import AnnotationError
        raise AnnotationError("bad_args", "a local web page is http://localhost:PORT (or 127.0.0.1:PORT)")
    path = parts.path or "/"
    return f"{parts.scheme}://{'[::1]' if host in ('::1', '[::1]') else host}:{parts.port}{path}" + (f"?{parts.query}" if parts.query else "")


# -- the injected script --------------------------------------------------------------
AGENT_JS = r"""
(function(){
  if (window.__faustusAnnotate) return; window.__faustusAnnotate = true;
  if (window.parent === window) return;
  var P = window.parent, picking = false, hover = null, marks = [];
  function post(m){ try { P.postMessage(Object.assign({source:'faustus-annotate'}, m), '*'); } catch(e){} }
  function sel(el){
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + CSS.escape(el.id);
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 8) {
      var tag = el.tagName.toLowerCase(), p = el.parentElement;
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      if (p) { var same = Array.prototype.filter.call(p.children, function(c){ return c.tagName === el.tagName; });
        if (same.length > 1) tag += ':nth-of-type(' + (same.indexOf(el) + 1) + ')'; }
      parts.unshift(tag); el = p;
    }
    return parts.join(' > ');
  }
  function box(el){ var r = el.getBoundingClientRect(); return {x:r.left, y:r.top, w:r.width, h:r.height}; }
  var style = document.createElement('style');
  style.textContent = '[data-faustus-hover]{outline:2px solid #e06c75!important;outline-offset:2px!important;cursor:crosshair!important}'
    + '[data-faustus-mark]{outline:2px dashed #e5c07b!important;outline-offset:2px!important}'
    + '.faustus-mark-badge{position:absolute;z-index:2147483647;background:#e5c07b;color:#1e1e1e;font:600 11px/16px system-ui,sans-serif;'
    + 'min-width:16px;height:16px;border-radius:8px;text-align:center;padding:0 4px;pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,.4)}';
  (document.head || document.documentElement).appendChild(style);
  function ready(){ post({type:'ready', url: location.href, title: document.title}); }
  document.addEventListener('mouseup', function(){
    if (picking) return;
    var s = window.getSelection(); var text = s ? String(s).trim() : '';
    if (!text) return;
    var node = s.anchorNode; var el = node && (node.nodeType === 1 ? node : node.parentElement);
    var r = s.rangeCount ? s.getRangeAt(0).getBoundingClientRect() : null;
    post({type:'select', quote: text.slice(0, 4000), selector: sel(el), url: location.href,
          box: r ? {x:r.left, y:r.top, w:r.width, h:r.height} : null});
  }, true);
  document.addEventListener('mouseover', function(e){
    if (!picking) return; if (hover) hover.removeAttribute('data-faustus-hover');
    hover = e.target; if (hover && hover.setAttribute) hover.setAttribute('data-faustus-hover', '');
  }, true);
  document.addEventListener('click', function(e){
    if (!picking) return; e.preventDefault(); e.stopPropagation();
    var el = e.target; if (hover) hover.removeAttribute('data-faustus-hover'); hover = null; picking = false;
    var text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('alt') || el.tagName.toLowerCase()).trim();
    post({type:'pick', quote: text.slice(0, 600), selector: sel(el), tag: el.tagName.toLowerCase(), url: location.href, box: box(el)});
  }, true);
  document.addEventListener('keydown', function(e){
    if (picking && e.key === 'Escape') { picking = false; if (hover) hover.removeAttribute('data-faustus-hover'); hover = null; post({type:'pick-cancel'}); }
  }, true);
  function paint(){
    marks.forEach(function(m){ if (m.badge && m.badge.parentNode) m.badge.parentNode.removeChild(m.badge); if (m.el) m.el.removeAttribute('data-faustus-mark'); });
    marks = marks.map(function(m){
      var el = null; try { el = m.selector ? document.querySelector(m.selector) : null; } catch(e){}
      if (!el) return {selector: m.selector, n: m.n};
      el.setAttribute('data-faustus-mark', '');
      var r = el.getBoundingClientRect(), b = document.createElement('span');
      b.className = 'faustus-mark-badge'; b.textContent = m.n;
      b.style.left = (r.left + window.scrollX - 6) + 'px'; b.style.top = (r.top + window.scrollY - 8) + 'px';
      document.body.appendChild(b); return {selector: m.selector, n: m.n, el: el, badge: b};
    });
  }
  window.addEventListener('message', function(e){
    if (e.source !== P) return; var d = e.data || {};
    if (d.source !== 'faustus-studio') return;
    if (d.type === 'pick') { picking = !!d.on; if (!picking && hover) { hover.removeAttribute('data-faustus-hover'); hover = null; } }
    if (d.type === 'marks') { marks.forEach(function(m){ if (m.badge && m.badge.parentNode) m.badge.parentNode.removeChild(m.badge); if (m.el) m.el.removeAttribute('data-faustus-mark'); });
      marks = (d.marks || []).map(function(m){ return {selector: m.selector, n: m.n}; }); paint(); }
    if (d.type === 'hello') ready();
  });
  var t = null; window.addEventListener('resize', function(){ clearTimeout(t); t = setTimeout(paint, 120); });
  new MutationObserver(function(){ clearTimeout(t); t = setTimeout(paint, 300); }).observe(document.documentElement, {childList:true, subtree:true});
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
})();
"""
_TAG = ("<script data-faustus-annotate>" + AGENT_JS + "</script>").encode("utf-8")


def inject(html: bytes) -> bytes:
    """The annotate script before ``</head>`` (or at the start of ``<body>``, or at the very top)."""
    low = html[:200_000].lower()
    for marker, before in ((b"</head>", True), (b"<body", False)):
        i = low.find(marker)
        if i >= 0:
            if not before:
                j = html.find(b">", i)
                if j < 0:
                    continue
                return html[: j + 1] + _TAG + html[j + 1:]
            return html[:i] + _TAG + html[i:]
    return _TAG + html


# -- HTTP plumbing ----------------------------------------------------------------------
def _split_head(head: bytes) -> Tuple[str, list]:
    lines = head.decode("latin-1").split("\r\n")
    return lines[0], [ln.split(":", 1) for ln in lines[1:] if ":" in ln]


def _join_head(first: str, headers: list) -> bytes:
    return (first + "\r\n" + "".join(f"{k}:{v}\r\n" for k, v in headers) + "\r\n").encode("latin-1")


async def _read_head(reader: asyncio.StreamReader) -> Tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _HEAD_LIMIT:
            break
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _dechunk(body: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(body):
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        size = int(body[i:j].split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out += body[j + 2: j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


async def _read_body(reader: asyncio.StreamReader, rest: bytes, headers: list) -> bytes:
    hmap = {k.strip().lower(): v.strip() for k, v in headers}
    body = rest
    if "chunked" in hmap.get("transfer-encoding", "").lower():
        while not body.endswith(b"0\r\n\r\n") and len(body) < _HTML_LIMIT:
            chunk = await reader.read(65536)
            if not chunk:
                break
            body += chunk
        return _dechunk(body)
    if "content-length" in hmap:
        want = int(hmap["content-length"] or 0)
        while len(body) < want:
            chunk = await reader.read(65536)
            if not chunk:
                break
            body += chunk
        return body[:want]
    while len(body) < _HTML_LIMIT:
        chunk = await reader.read(65536)
        if not chunk:
            break
        body += chunk
    return body


async def _connect(hosts: list, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The first loopback address that answers, remembered at the front of ``hosts``. ``localhost`` is tried as
    127.0.0.1 first: on Windows a refused connection to ::1 takes about two seconds, which delayed every request
    and the hot-reload socket of a dev server that only listens on IPv4."""
    last: Optional[OSError] = None
    for host in list(hosts):
        try:
            pair = await asyncio.open_connection(host, port)
        except OSError as exc:
            last = exc
            continue
        if hosts[0] != host:
            hosts.remove(host)
            hosts.insert(0, host)
        return pair
    raise last or OSError("no address")


def _handler(upstream: str, local_port_ref: list):
    up = urlsplit(upstream)
    up_host, up_port = (up.hostname or "localhost").strip("[]"), up.port or 80
    up_hosts = ["127.0.0.1", "::1"] if up_host == "localhost" else [up_host]
    up_origin = f"{up.scheme}://{up.netloc}"

    async def handle(creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        mine = f"http://127.0.0.1:{local_port_ref[0]}"
        try:
            head, rest = await _read_head(creader)
            if not head:
                cwriter.close()
                return
            first, headers = _split_head(head)
            upgrade = any(k.strip().lower() == "upgrade" for k, _ in headers)
            out = []
            for k, v in headers:
                key = k.strip().lower()
                if key == "host":
                    v = f" {up.netloc}"
                elif key in ("origin", "referer"):
                    v = v.replace(mine, up_origin)
                elif key == "accept-encoding" and not upgrade:
                    v = " identity"
                elif key in ("connection", "keep-alive", "proxy-connection") and not upgrade:
                    continue
                out.append((k, v))
            if not upgrade:
                out.append(("Connection", " close"))
            try:
                ureader, uwriter = await _connect(up_hosts, up_port)
            except OSError:
                msg = f"Nothing answers at {upstream} (is the dev server running?)".encode("utf-8")
                cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
                              + str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg)
                await cwriter.drain()
                cwriter.close()
                return
            uwriter.write(_join_head(first, out) + rest)
            await uwriter.drain()
            if upgrade:
                await asyncio.gather(_pipe(creader, uwriter), _pipe(ureader, cwriter))
                return
            to_up = asyncio.ensure_future(_pipe(creader, uwriter))
            rhead, rrest = await _read_head(ureader)
            status, rheaders = _split_head(rhead)
            kept = []
            html = False
            for k, v in rheaders:
                key = k.strip().lower()
                if key in ("x-frame-options", "content-security-policy", "content-security-policy-report-only"):
                    continue
                if key == "location":
                    v = v.replace(up_origin, mine)
                if key == "content-type" and "text/html" in v.lower():
                    html = True
                kept.append((k, v))
            code = status.split(" ")[1] if len(status.split(" ")) > 1 else ""
            if html and code == "200":
                body = await _read_body(ureader, rrest, rheaders)
                body = inject(body)
                kept = [(k, v) for k, v in kept if k.strip().lower() not in ("content-length", "transfer-encoding", "connection")]
                kept += [("Content-Length", f" {len(body)}"), ("Connection", " close"), ("Cache-Control", " no-store")]
                cwriter.write(_join_head(status, kept) + body)
                await cwriter.drain()
                cwriter.close()
                uwriter.close()
            else:
                cwriter.write(_join_head(status, kept) + rrest)
                await cwriter.drain()
                await _pipe(ureader, cwriter)
            to_up.cancel()
        except (ConnectionError, OSError, ValueError, asyncio.CancelledError):
            try:
                cwriter.close()
            except Exception:  # noqa: BLE001
                pass

    return handle


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is not None and _thread is not None and _thread.is_alive():
            return _loop
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            started.set()
            loop.run_forever()

        _thread = threading.Thread(target=run, name="annotation-proxy", daemon=True)
        _thread.start()
        started.wait(5)
        _loop = loop
        return loop


def ensure(url: str) -> Dict[str, str]:
    """A proxy for the URL's origin (one per origin, reused), and the URL to frame."""
    upstream = check_upstream(url)
    parts = urlsplit(upstream)
    origin = f"{parts.scheme}://{parts.netloc}"
    loop = _ensure_loop()
    with _lock:
        hit = _proxies.get(origin)
    if hit is None:
        ref = [0]

        async def start():
            return await asyncio.start_server(_handler(origin + "/", ref), "127.0.0.1", 0)

        server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        ref[0] = server.sockets[0].getsockname()[1]
        with _lock:
            _proxies[origin] = (server, ref[0])
        hit = (server, ref[0])
    path = parts.path or "/"
    framed = f"http://127.0.0.1:{hit[1]}{path}" + (f"?{parts.query}" if parts.query else "")
    return {"upstream": upstream, "url": framed, "origin": f"http://127.0.0.1:{hit[1]}"}


# -- HTML files and documents ---------------------------------------------------------------
_static: Optional[Tuple[asyncio.AbstractServer, int]] = None
_roots: Dict[str, object] = {}   # token -> folder (str) or {"name": str, "html": bytes}


def _static_handler():
    import mimetypes
    import os
    from urllib.parse import unquote

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head, _ = await _read_head(reader)
            first, _headers = _split_head(head)
            parts = first.split(" ")
            raw_path = unquote(parts[1].split("?", 1)[0]) if len(parts) > 1 else "/"
            token, _, rel = raw_path.lstrip("/").partition("/")
            root = _roots.get(token)
            body, ctype, code = b"Not found", "text/plain; charset=utf-8", "404 Not Found"
            if isinstance(root, dict):
                if rel in ("", root["name"]):
                    body, ctype, code = inject(root["html"]), "text/html; charset=utf-8", "200 OK"
            elif isinstance(root, str):
                full = os.path.normpath(os.path.join(root, *[p for p in rel.split("/") if p not in ("", ".", "..")]))
                if full.startswith(os.path.normpath(root)) and os.path.isfile(full):
                    with open(full, "rb") as f:
                        body = f.read()
                    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                    if ctype == "text/html":
                        body, ctype = inject(body), "text/html; charset=utf-8"
                    code = "200 OK"
            writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                         "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode("latin-1") + body)
            await writer.drain()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    return handle


def _static_port() -> int:
    global _static
    loop = _ensure_loop()
    with _lock:
        if _static is not None:
            return _static[1]

    async def start():
        return await asyncio.start_server(_static_handler(), "127.0.0.1", 0)

    server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    with _lock:
        if _static is None:
            _static = (server, server.sockets[0].getsockname()[1])
        return _static[1]


def serve_file(path: str) -> Dict[str, str]:
    """An HTML file of the workspace, framable, with its folder's relative assets (one token per folder)."""
    import os
    import secrets
    full = os.path.abspath(str(path or ""))
    if not os.path.isfile(full):
        from src.annotations import AnnotationError
        raise AnnotationError("not_found", f"no such file: {path}")
    folder, name = os.path.split(full)
    port = _static_port()
    with _lock:
        token = next((k for k, v in _roots.items() if v == folder), None) or secrets.token_hex(6)
        _roots[token] = folder
    from urllib.parse import quote
    return {"url": f"http://127.0.0.1:{port}/{token}/{quote(name)}", "origin": f"http://127.0.0.1:{port}", "upstream": full}


def serve_html(content: str, name: str = "index.html") -> Dict[str, str]:
    """HTML given as text (a Studio document), framable with the annotate script."""
    import secrets
    port = _static_port()
    token = secrets.token_hex(6)
    with _lock:
        docs = [k for k, v in _roots.items() if isinstance(v, dict)]
        for old in docs[:-50]:
            _roots.pop(old, None)
        _roots[token] = {"name": "index.html", "html": str(content or "").encode("utf-8")}
    return {"url": f"http://127.0.0.1:{port}/{token}/index.html", "origin": f"http://127.0.0.1:{port}", "upstream": name}


def stop_all() -> None:
    with _lock:
        items = list(_proxies.values())
        _proxies.clear()
        loop = _loop
    for server, _port in items:
        if loop is not None:
            loop.call_soon_threadsafe(server.close)
