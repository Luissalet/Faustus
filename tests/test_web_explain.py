"""web_explain: source maps, static bundle analysis and a live observation of a small local site.

The site is served from a thread on 127.0.0.1: one button that fetches /api/hello, formats the answer with a function
from a "package" and appends a paragraph. Its bundle carries a hand-built source map with two original files, one of
them under node_modules, so every layer can be checked against known answers.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.web_explain import WebExplainError, analyze_bundle, explain_feature, trace_location
from src.web_explain.capture import check_url, redact
from src.web_explain.sourcemap import (SourceMap, banner_versions, decode_vlq, encode_vlq, map_url_from_code,
                                       package_of)

LIB = ['function formatGreeting(name) {', '  return "Hello, " + name + "!";', '}']
APP = [
    'import { formatGreeting } from "tiny-format";',
    'async function loadGreeting() {',
    '  const res = await fetch("/api/hello?who=world");',
    '  const data = await res.json();',
    '  return formatGreeting(data.name);',
    '}',
    'async function onClick() {',
    '  const text = await loadGreeting();',
    '  const p = document.createElement("p");',
    '  p.id = "greeting";',
    '  p.textContent = text;',
    '  document.body.appendChild(p);',
    '}',
    'document.getElementById("go").addEventListener("click", onClick);',
]
SOURCES = ["webpack://demo/./node_modules/.pnpm/tiny-format@1.2.3/node_modules/tiny-format/index.js",
           "webpack://demo/./src/app.js"]
# Generated line -> (source index, original 0-based line). The import line of the app is not in the bundle.
LINES = [(0, 0), (0, 1), (0, 2)] + [(1, i) for i in range(1, len(APP))]


def build_bundle():
    body = [LIB[i] if s == 0 else APP[i] for s, i in LINES]
    segs, prev_s, prev_l = [], 0, 0
    for s, line in LINES:
        segs.append(encode_vlq([0, s - prev_s, line - prev_l, 0]))
        prev_s, prev_l = s, line
    smap = {"version": 3, "file": "bundle.js", "sources": SOURCES, "names": [],
            "sourcesContent": ["\n".join(LIB), "\n".join(APP)], "mappings": ";".join(segs)}
    code = "/*! tiny-format v1.2.3 | MIT */\n" + "\n".join(body) + "\n//# sourceMappingURL=bundle.js.map\n"
    # The banner adds one generated line in front: shift the map by one empty line.
    smap["mappings"] = ";" + smap["mappings"]
    return code, smap


PAGE = '<!doctype html><html lang="en"><head><title>Greeter</title></head><body><button id="go">Greet</button>' \
       '<script src="/bundle.js"></script></body></html>'


@pytest.fixture(scope="module")
def site():
    code, smap = build_bundle()
    routes = {"/": ("text/html", PAGE), "/bundle.js": ("application/javascript", code),
              "/bundle.js.map": ("application/json", json.dumps(smap))}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/hello":
                ctype, text = "application/json", json.dumps({"name": "world"})
            elif path in routes:
                ctype, text = routes[path]
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_vlq_round_trip():
    assert decode_vlq("AAAA") == [0, 0, 0, 0]
    assert decode_vlq("gB") == [16]
    assert decode_vlq("D") == [-1]
    for values in ([0], [1, -1, 15, -16, 1000, -123456], [2 ** 20]):
        assert decode_vlq(encode_vlq(values)) == values
    with pytest.raises(ValueError):
        decode_vlq("g")


def test_map_lookup_snippet_and_inventory():
    _, data = build_bundle()
    m = SourceMap(data, "bundle.js.map")
    assert m.sources == ["node_modules/.pnpm/tiny-format@1.2.3/node_modules/tiny-format/index.js", "src/app.js"]
    hit = m.lookup(2, 0)  # line 1 is the banner
    assert hit["source"].endswith("tiny-format/index.js") and hit["line"] == 1
    hit = m.lookup(6, 25)  # the fetch line, any column
    assert hit["source"] == "src/app.js" and hit["line"] == 3
    assert m.lookup(1, 0) is None
    assert ">3 |   const res = await fetch" in m.snippet(hit["index"], 3, 1)
    assert m.first_party() == ["src/app.js"]
    assert m.packages() == [{"name": "tiny-format", "version": "1.2.3", "files": 1}]
    assert m.search("createElement")[0] == {"source": "src/app.js", "line": 9, "text": 'const p = document.createElement("p");'}


def test_index_map_sections():
    _, data = build_bundle()
    index = {"version": 3, "sections": [{"offset": {"line": 100, "column": 0}, "map": data}]}
    m = SourceMap(index)
    assert m.lookup(106, 3)["line"] == 3
    assert m.lookup(5, 0) is None
    assert m.packages()[0]["name"] == "tiny-format"


@pytest.mark.parametrize("path,expected", [
    ("webpack://x/./node_modules/react-dom/cjs/react-dom.production.min.js", ("react-dom", None)),
    ("../node_modules/@tanstack/react-query/build/index.js", ("@tanstack/react-query", None)),
    ("node_modules/.pnpm/@scope+pkg@2.0.1_react@18.2.0/node_modules/@scope/pkg/x.js", ("@scope/pkg", "2.0.1")),
    ("node_modules/.pnpm/react-dom@18.2.0_react@18.2.0/node_modules/react-dom/index.js", ("react-dom", "18.2.0")),
    ("/node_modules/.vite/deps/react-dom_client.js?v=1a2b", ("react-dom", None)),
    ("/node_modules/.vite/deps/@tanstack_react-query.js", ("@tanstack/react-query", None)),
    ("/node_modules/.vite/deps/chunk-ABCD.js", None),
    ("src/components/Search.tsx", None),
])
def test_package_of(path, expected):
    assert package_of(path) == expected


def test_banners_and_map_comment():
    code = "/*! jQuery v3.7.1 | (c) OpenJS */ x; /** @license React v18.3.1 */ y; /*! lodash 4.17.21 */"
    assert banner_versions(code) == [{"name": "jQuery", "version": "3.7.1"}, {"name": "React", "version": "18.3.1"},
                                     {"name": "lodash", "version": "4.17.21"}]
    assert map_url_from_code("a()\n//# sourceMappingURL=app.js.map", "https://x.test/js/app.js") == "https://x.test/js/app.js.map"
    assert map_url_from_code("a()", "https://x.test/a.js") is None


def test_url_and_redaction():
    with pytest.raises(WebExplainError):
        check_url("file:///etc/passwd")
    with pytest.raises(WebExplainError):
        check_url("javascript:alert(1)")
    masked = redact('{"user":"ana","password":"hunter2","api_key":"k-1"}')
    assert "hunter2" not in masked and "k-1" not in masked and "ana" in masked
    assert "abc" not in redact("q=1&token=abc")


def test_trace_location_and_bundle_analysis(site):
    out = trace_location(site + "/bundle.js", 6, 22, context=1)
    assert out["original"]["source"] == "src/app.js" and out["original"]["line"] == 3
    assert "fetch(\"/api/hello" in out["snippet"]
    lib = trace_location(site + "/bundle.js", 3, 4)
    assert lib["original"]["package"] == "tiny-format" and lib["original"]["package_version"] == "1.2.3"

    page = analyze_bundle(site + "/", search="appendChild")
    assert page["scripts_found"] == 1
    rep = page["analysed"][0]
    assert rep["source_map"].endswith("/bundle.js.map") and rep["first_party_modules"] == ["src/app.js"]
    assert rep["license_banners"] == [{"name": "tiny-format", "version": "1.2.3"}]
    assert rep["search"]["hits"][0]["source"] == "src/app.js"
    assert page["packages"] == [{"name": "tiny-format", "version": "1.2.3"}]


def _browser_ok():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _browser_ok(), reason="no Playwright Chromium here")
def test_explain_feature_live(site, tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_WEB_EXPLAIN_DIR", str(tmp_path))
    out = explain_feature(site + "/", "How does the greeting get its name?", [{"type": "click", "target": "Greet"}],
                          wait_ms=500)
    claims = {e["id"]: e for e in out["evidence"]}
    text = json.dumps(out)
    req = next(e for e in out["evidence"] if e["claim"].startswith("R1:"))
    assert "/api/hello?who=world -> 200 (application/json)" in req["claim"]
    assert "issued from src/app.js:3" in req["claim"]
    assert '"name": "world"' in req["response"] or '"name":"world"' in req["response"]
    assert any("1 nodes added" in e["claim"] or "nodes added" in e["claim"] for e in out["evidence"])
    ran = next(e for e in out["evidence"] if e["claim"].startswith("Site code that ran"))
    assert "onClick in src/app.js:7" in ran["claim"] and "loadGreeting in src/app.js:2" in ran["claim"]
    assert any(e["claim"].startswith("Library code that ran") and "tiny-format" in e["claim"] for e in out["evidence"])
    inferred = [e for e in out["evidence"] if e["status"] == "inferred"]
    assert any("server endpoint" in e["claim"] and req["id"] in e["rests_on"] for e in inferred)
    assert all(r in claims for e in inferred for r in e.get("rests_on", []))
    assert "tiny-format@1.2.3" in text
    assert any("greeting" in e["claim"].lower() and e["claim"].startswith("'greeting'") for e in out["evidence"])
    assert out["unknown"] and "server" in out["unknown"][0]
    assert out["screenshots"]["after"].endswith("after.png")

    with pytest.raises(WebExplainError):
        explain_feature(site + "/", "", None)


def test_server_tools_never_raise():
    from mcp_servers import web_explain_server as srv

    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert names == ["explain_feature", "observe_web_execution", "analyze_web_bundle", "trace_web_source_location"]
    for t in srv.TOOLS:
        assert len(t.description) <= 600
    out = asyncio.run(srv.call_tool("analyze_web_bundle", {"url": "file:///x"}))
    assert out[0].text.startswith("Error in analyze_web_bundle")
    out = asyncio.run(srv.call_tool("trace_web_source_location", {}))
    assert "missing argument" in out[0].text
    assert asyncio.run(srv.call_tool("nope", {}))[0].text == "Unknown tool: nope"

def test_registered_as_builtin_server():
    from pathlib import Path

    from src import builtin_mcp

    script, label = builtin_mcp._BUILTIN_SERVERS["web_explain"]
    assert script == "mcp_servers/web_explain_server.py" and (Path(__file__).parent.parent / script).is_file()
    assert "web_explain" not in builtin_mcp.NATIVE_TWIN_SERVERS


def test_search_puts_code_before_imports():
    _, data = build_bundle()
    hits = SourceMap(data).search("formatGreeting", limit=2)
    assert [h["line"] for h in hits if h["source"] == "src/app.js"][0] == 5  # the call, not the import on line 1
    assert SourceMap({"version": 3, "sources": ["virtual:pwa-register", "src/a.js"], "mappings": ""}).first_party() == ["src/a.js"]


def test_bot_check_titles_are_recognised():
    from src.web_explain.capture import BOT_CHECK

    for title in ("Just a moment...", "Attention Required! | Cloudflare", "Verify you are human"):
        assert BOT_CHECK.search(title)
    assert not BOT_CHECK.search("Greeter")


def test_index_map_snippets_come_from_the_right_section():
    a = {"version": 3, "sources": ["src/a.js"], "sourcesContent": ["const a = 1;"], "names": [], "mappings": "AAAA"}
    b = {"version": 3, "sources": ["src/b.js"], "sourcesContent": ["const b = 2;"], "names": [], "mappings": "AAAA"}
    m = SourceMap({"version": 3, "sections": [{"offset": {"line": 0, "column": 0}, "map": a},
                                               {"offset": {"line": 1, "column": 0}, "map": b}]})
    hit = m.lookup(2, 0)
    assert hit["source"] == "src/b.js" and hit["index"] == 1
    assert "const b = 2;" in m.snippet(hit["index"], hit["line"], 0)
    assert "const a = 1;" in m.snippet(m.lookup(1, 0)["index"], 1, 0)


def test_script_positions_count_utf16_units_like_the_protocol():
    from src.web_explain.capture import _Script

    s = _Script({"scriptId": "1", "url": "https://x.test/a.js"})
    s.source = "\U0001F600" * 100 + "\nfunction f() {}\u2028x"
    start = 200 + 1  # 100 astral characters are 200 UTF-16 units, then the line break
    assert s.position(start) == (1, 0)
    assert s.position(start + 9) == (1, 9)  # "f" of the function name
    assert s.position(start + 16) == (2, 0)  # past U+2028, a JavaScript line terminator
    s2 = _Script({"scriptId": "2", "url": "https://x.test/b.js"})
    s2.source = "var a = '\U0001F600'; function g() {}"
    assert s2.position(14) == (0, 14)  # "function" starts at UTF-16 unit 14: the emoji before it counts twice


@pytest.mark.skipif(not _browser_ok(), reason="no Playwright Chromium here")
def test_every_ordered_action_runs(site, tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_WEB_EXPLAIN_DIR", str(tmp_path))
    actions = [{"type": "wait", "value": 1}] * 6 + [{"type": "click", "target": "Greet"}]
    out = explain_feature(site + "/", "What does Greet fetch?", actions, wait_ms=300)
    did = next(e for e in out["evidence"] if e["claim"].startswith("Actions performed"))
    assert did["claim"].count("wait 1 ms") == 6 and "click 'Greet'" in did["claim"]
    assert any(e["claim"].startswith("R1:") and "/api/hello" in e["claim"] for e in out["evidence"])
