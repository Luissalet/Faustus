"""Hub browser lanes in Faustus (Agora #265): one app at a time per site.

A stub Hub (a tiny HTTP server speaking the lane endpoints) stands in for the real one; the vendored client library does the
talking. Covers the bounded reads (``reach_browser`` read / capture), the shared browser MCP (``browser_navigate`` keeps the
lane of its site until it moves on, closes or idles) and the no-op paths (Hub away, lanes off)."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from src import browser_lanes as bl
from src import mcp_manager as mm
from src.mcp_manager import McpManager
from src.reach import browser as b

URL = "https://shop.example.co.uk/p/1"
SINCE = 1_790_000_000.0


@pytest.fixture()
def hub(monkeypatch):
    """A stub Hub with lane endpoints; ``state.busy`` makes ``acquire`` answer that another app holds the lane."""
    state = SimpleNamespace(posts=[], busy=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._reply({"ok": False}, 404)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            state.posts.append((self.path, body))
            if self.path == "/api/lane/acquire":
                if state.busy:
                    return self._reply({"ok": True, "granted": False, "busy": True, "queue_seq": 3, "waiting": 0, **state.busy})
                n = sum(1 for p, _ in state.posts if p == "/api/lane/acquire")
                return self._reply({"ok": True, "granted": True, "busy": False, "lease_id": f"L{n}", "domain": body["domain"]})
            return self._reply({"ok": True, "released": True})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("HOARD_HUB_URL", state.url)
    monkeypatch.setenv("FAUSTUS_BROWSER_LANES", "1")
    monkeypatch.setattr(bl, "_down_until", 0.0)
    bl.release_all()
    state.paths = lambda: [p for p, _ in state.posts]
    state.acquired = lambda: [b_["domain"] for p, b_ in state.posts if p == "/api/lane/acquire"]
    yield state
    bl.release_all()
    srv.shutdown()
    srv.server_close()


def _dead_hub(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("HOARD_HUB_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("FAUSTUS_BROWSER_LANES", "1")
    monkeypatch.setattr(bl, "_down_until", 0.0)


def _page(monkeypatch, calls):
    fr = SimpleNamespace(error="", status=200, text="<h1>Hi</h1>", final_url=URL, content_type="text/html")

    def fetch(url, **kw):
        calls.append(url)
        return fr

    monkeypatch.setattr(b, "browser_for", lambda *_: SimpleNamespace(fetch=fetch))


# ── bounded reads ───────────────────────────────────────────────────────────

def test_a_free_site_is_read_inside_its_lane(hub, monkeypatch):
    calls = []
    _page(monkeypatch, calls)
    assert str(b.read_response(URL).url) == URL
    assert calls == [URL]
    assert hub.acquired() == ["example.co.uk"]
    assert hub.paths() == ["/api/lane/acquire", "/api/lane/release"]
    assert hub.posts[0][1]["holder"] == "faustus"


def test_a_busy_site_is_not_read_and_says_who_holds_it(hub, monkeypatch):
    calls = []
    _page(monkeypatch, calls)
    hub.busy = {"held_by": "tantalus", "since": SINCE, "expires_in_s": 40}
    with pytest.raises(bl.BrowserLaneBusy) as err:
        b.read_response(URL)
    text = str(err.value)
    assert text.startswith("ocupado por tantalus desde las ") and "example.co.uk" in text
    assert err.value.held_by == "tantalus"
    assert isinstance(err.value, ValueError)      # the reach tools already turn a ValueError into «reach_browser: <reason>»
    assert calls == []
    assert "/api/lane/release" not in hub.paths()


def test_a_busy_site_blocks_a_capture_too(hub, monkeypatch):
    monkeypatch.setattr(b, "browser_for", lambda *_: pytest.fail("must not open"))
    monkeypatch.setattr("src.hoard_link.web.safety.check_url", lambda *_: None)
    hub.busy = {"held_by": "argus", "since": SINCE, "expires_in_s": 40}
    with pytest.raises(ValueError, match="ocupado por argus"):
        b.capture(b.BrowserRequest(action="capture", url=URL, endpoint_contains="/api"))


def test_the_lane_is_given_back_when_the_read_fails(hub, monkeypatch):
    fr = SimpleNamespace(error="boom", status=0, text="", final_url=URL, content_type="")
    monkeypatch.setattr(b, "browser_for", lambda *_: SimpleNamespace(fetch=lambda *a, **k: fr))
    with pytest.raises(ValueError, match="boom"):
        b.read_response(URL)
    assert hub.paths() == ["/api/lane/acquire", "/api/lane/release"]


def test_hub_away_the_read_goes_ahead_and_is_not_asked_again_for_a_while(monkeypatch):
    _dead_hub(monkeypatch)
    calls = []
    _page(monkeypatch, calls)
    assert str(b.read_response(URL).url) == URL
    assert bl.enabled() is False                    # remembered: the next read does not pay a refused connect
    assert str(b.read_response(URL).url) == URL
    assert calls == [URL, URL]


def test_lanes_switched_off_never_talk_to_the_hub(hub, monkeypatch):
    monkeypatch.setenv("FAUSTUS_BROWSER_LANES", "0")
    calls = []
    _page(monkeypatch, calls)
    b.read_response(URL)
    assert calls == [URL] and hub.posts == []


def test_no_hub_configured_means_no_lanes(monkeypatch):
    monkeypatch.delenv("FAUSTUS_BROWSER_LANES", raising=False)
    monkeypatch.delenv("HOARD_HUB_URL", raising=False)
    monkeypatch.delenv("HOARD_HUB_TOKEN_FILE", raising=False)
    monkeypatch.setattr("src.hoard_hub.hub_location", lambda: {"configured": False, "url": ""})
    assert bl.enabled() is False
    with bl.read_lane(URL):
        pass


def test_urls_without_a_site_take_no_lane(hub):
    for url in ("about:blank", "data:text/html,hi", "/relative", "", "file:///x"):
        with bl.read_lane(url):
            pass
    assert hub.posts == []


# ── the shared browser MCP ──────────────────────────────────────────────────

def test_navigate_keeps_the_lane_until_it_moves_to_another_site(hub):
    assert bl.guard_navigate("c1", URL) is None
    assert bl.held_lanes()["c1"]["domain"] == "example.co.uk"
    assert bl.guard_navigate("c1", "https://shop.example.co.uk/p/2") is None      # same site: nothing new asked
    assert hub.acquired() == ["example.co.uk"]
    assert bl.guard_navigate("c1", "https://other.test/") is None                  # other site: old one back, new one taken
    assert hub.acquired() == ["example.co.uk", "other.test"]
    assert hub.paths() == ["/api/lane/acquire", "/api/lane/acquire", "/api/lane/release"] or \
        hub.paths() == ["/api/lane/acquire", "/api/lane/release", "/api/lane/acquire"]
    assert bl.held_lanes()["c1"]["domain"] == "other.test"


def test_navigate_to_a_site_another_app_holds_is_blocked_with_who_and_since(hub):
    hub.busy = {"held_by": "tantalus", "since": SINCE, "expires_in_s": 40}
    out = bl.guard_navigate("c1", URL)
    assert out["blocked"] is True and out["busy"] is True and out["policy"] == "browser_lane"
    assert out["held_by"] == "tantalus" and out["domain"] == "example.co.uk"
    assert out["error"].startswith("browser_navigate: ocupado por tantalus desde las ")
    assert "c1" not in bl.held_lanes()


def test_leaving_a_site_frees_it_even_when_the_next_one_is_busy(hub):
    assert bl.guard_navigate("c1", URL) is None
    hub.busy = {"held_by": "argus", "since": SINCE, "expires_in_s": 40}
    assert bl.guard_navigate("c1", "https://other.test/")["busy"] is True
    assert "c1" not in bl.held_lanes()
    assert hub.paths().count("/api/lane/release") == 1


def test_closing_the_browser_and_idling_give_the_lane_back(hub):
    assert asyncio.run(bl.guard_call("c1", "browser_navigate", {"url": URL})) is None
    assert asyncio.run(bl.guard_call("c1", "browser_snapshot", {})) is None
    assert "c1" in bl.held_lanes()
    assert asyncio.run(bl.guard_call("c1", "browser_close", {})) is None
    assert bl.held_lanes() == {} and hub.paths()[-1] == "/api/lane/release"

    assert bl.guard_navigate("c2", URL) is None
    assert bl.sweep(time.monotonic() + 1) == 0                                      # recently used: stays
    assert bl.sweep(time.monotonic() + bl.IDLE_RELEASE_S + 5) == 1                  # idle: given back
    assert bl.held_lanes() == {}
    assert hub.paths().count("/api/lane/release") == 2


def test_a_snapshot_counts_as_use_so_an_active_browser_keeps_its_lane(hub):
    assert bl.guard_navigate("c1", URL) is None
    h = bl._held["c1"]
    h.last_used -= 1000
    bl.touch("c1")
    assert bl.sweep() == 0


def test_navigate_with_the_hub_away_goes_ahead(monkeypatch):
    _dead_hub(monkeypatch)
    assert bl.guard_navigate("c1", URL) is None
    assert bl.held_lanes() == {}


# ── dispatch (McpManager.call_tool) ─────────────────────────────────────────

class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self):
        self.content = [_Content("### Page\n- ok")]
        self.isError = False


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return _Result()


def _manager(server_id="builtin_browser"):
    m = McpManager()
    session = _Session()
    m._sessions[server_id] = session
    m._connections[server_id] = {"status": "connected", "name": server_id}
    return m, session


def test_dispatch_answers_who_holds_the_site_without_calling_the_browser(hub):
    hub.busy = {"held_by": "tantalus", "since": SINCE, "expires_in_s": 40}
    m, session = _manager()
    out = asyncio.run(m.call_tool("mcp__builtin_browser__browser_navigate", {"url": URL}))
    assert out["blocked"] is True and out["busy"] is True and "ocupado por tantalus" in out["error"]
    assert session.calls == []


def test_dispatch_goes_ahead_and_holds_the_lane_when_the_site_is_free(hub):
    m, session = _manager()
    asyncio.run(m.call_tool("mcp__builtin_browser__browser_navigate", {"url": URL}))
    assert [c[0] for c in session.calls] == ["browser_navigate"]
    assert bl.held_lanes()["builtin_browser"]["domain"] == "example.co.uk"
    asyncio.run(m.call_tool("mcp__builtin_browser__browser_close", {}))
    assert bl.held_lanes() == {}


def test_other_servers_never_take_lanes(hub):
    m, session = _manager("some_other_server")
    asyncio.run(m.call_tool("mcp__some_other_server__browser_navigate", {"url": URL}))
    assert hub.posts == [] and len(session.calls) == 1


def test_busy_text_without_a_time_or_name():
    assert bl.busy_text("a.test") == "ocupado por otra app: el carril de navegador de a.test lo tiene esa app del Hub (la está leyendo ahora mismo)"
