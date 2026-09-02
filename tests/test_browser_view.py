"""Live browser view (audit finding 6, UI half).

`src/browser_view.py` takes one viewport frame after each browser ACTION and
returns {url, title, screenshot}; the agent loop turns it into a
`browser_view` SSE event for the Browser panel (static/js/browserView.js).
The frame is for the UI only — it must never land in the tool result's
`images` (what the model reads).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import browser_view as bv


ROOT = Path(__file__).resolve().parent.parent
P = "mcp__builtin_browser__"
JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="

NAV_TEXT = (
    "### Ran Playwright code\n```js\nawait page.goto('https://example.com/');\n```\n"
    "### Page\n- Page URL: https://example.com/\n- Page Title: Example Domain\n"
    "### Snapshot\n- [Snapshot](.playwright-mcp/page.yml)\n"
)
TABS_TEXT = "### Result\n- 0: (current) [Example Domain](https://example.com/)\n- 1: [Other](https://other.test/)\n"


class FakeMgr:
    def __init__(self, shot=None, tabs=None):
        self.calls = []
        self._shot = shot if shot is not None else {
            "stdout": "### Result\n- [Screenshot of viewport](.playwright-mcp/page.jpeg)",
            "exit_code": 0,
            "images": [{"data": JPEG, "mimeType": "image/jpeg"}],
        }
        self._tabs = tabs if tabs is not None else {"stdout": TABS_TEXT, "exit_code": 0}

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name.endswith("browser_take_screenshot"):
            return self._shot
        if name.endswith("browser_tabs"):
            return self._tabs
        return {"error": "unexpected", "exit_code": 1}


# ── parsing ───────────────────────────────────────────────────────────────


def test_parse_page_info_reads_playwright_lines():
    assert bv.parse_page_info(NAV_TEXT) == ("https://example.com/", "Example Domain")
    assert bv.parse_page_info("### Page\n- Page URL: about:blank\n") == ("about:blank", "")
    assert bv.parse_page_info("") == ("", "")
    assert bv.parse_page_info(None) == ("", "")


def test_parse_tabs_current():
    assert bv.parse_tabs_current(TABS_TEXT) == ("https://example.com/", "Example Domain")
    assert bv.parse_tabs_current("### Result\n- 0: [x](y)\n") == ("", "")


def test_action_classification():
    for name in ("browser_navigate", "browser_navigate_back", "browser_click", "browser_type", "browser_fill_form",
                 "browser_select_option", "browser_press_key", "browser_hover", "browser_drag", "browser_drop",
                 "browser_tabs", "browser_handle_dialog", "browser_mouse_click_xy", "browser_mouse_wheel"):
        assert bv.is_browser_action(P + name), name
    for name in ("browser_snapshot", "browser_take_screenshot", "browser_find", "browser_console_messages",
                 "browser_network_requests", "browser_wait_for", "browser_close"):
        assert not bv.is_browser_action(P + name), name
    assert not bv.is_browser_action("bash")
    assert not bv.is_browser_action(None)


# ── after_browser_action ──────────────────────────────────────────────────


async def test_event_shape_after_navigate_uses_result_text_for_url_title():
    mgr = FakeMgr()
    result = {"stdout": NAV_TEXT, "exit_code": 0}
    out = await bv.after_browser_action(P + "browser_navigate", result, mgr, {"browser_live_view": True})

    assert out == {
        "url": "https://example.com/",
        "title": "Example Domain",
        "screenshot": f"data:image/jpeg;base64,{JPEG}",
    }
    # viewport jpeg only; url/title came from the result → no tabs call
    assert mgr.calls == [(P + "browser_take_screenshot", {"type": "jpeg"})]
    # the action's own result is untouched (nothing for the model)
    assert "images" not in result


async def test_falls_back_to_tabs_list_when_result_has_no_page_lines():
    mgr = FakeMgr()
    out = await bv.after_browser_action(P + "browser_click", {"stdout": "### Ran Playwright code\n```js\nawait page.click()\n```", "exit_code": 0}, mgr, {})
    assert out["url"] == "https://example.com/"
    assert out["title"] == "Example Domain"
    assert [c[0] for c in mgr.calls] == [P + "browser_take_screenshot", P + "browser_tabs"]
    assert mgr.calls[1][1] == {"action": "list"}


async def test_no_frame_for_observation_tools_or_when_disabled_or_blocked():
    mgr = FakeMgr()
    assert await bv.after_browser_action(P + "browser_snapshot", {"stdout": NAV_TEXT}, mgr, {}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, mgr, {"browser_live_view": False}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"error": "x", "blocked": True, "exit_code": 1}, mgr, {}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, None, {}) is None
    assert mgr.calls == []


async def test_screenshot_failure_is_swallowed():
    class Boom(FakeMgr):
        async def call_tool(self, name, args):
            raise RuntimeError("browser gone")

    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, Boom(), {}) is None
    no_image = FakeMgr(shot={"stdout": "### Error\nno page", "exit_code": 1})
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, no_image, {}) is None


async def test_control_characters_are_stripped_from_url_and_title():
    mgr = FakeMgr()
    text = "### Page\n- Page URL: https://e.test/\x1b[31m\n- Page Title: Bad\x00Title\n"
    out = await bv.after_browser_action(P + "browser_navigate", {"stdout": text}, mgr, {})
    assert "\x1b" not in out["url"] and "\x00" not in out["title"]


# ── wiring: agent loop + chat route whitelist ─────────────────────────────


def test_agent_loop_emits_browser_view_event_and_attaches_frame_to_card():
    src = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "from src.browser_view import after_browser_action" in src
    assert '"type": "browser_view", "tool": block.tool_type, **_browser_view' in src
    # the card gets the frame, the model does not (no result["images"] write)
    assert 'tool_output_data["screenshot"] = _browser_view["screenshot"]' in src
    assert 'result["images"] = ' not in src.split("Live browser view")[1].split("Forward a file-write diff")[0]


def test_chat_route_forwards_browser_view_events():
    src = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    assert '"browser_view",' in src


# ── frontend ──────────────────────────────────────────────────────────────

_HAS_NODE = shutil.which("node") is not None


def _node(source: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_browser_view_js_parses():
    subprocess.run(["node", "--check", str(ROOT / "static" / "js" / "browserView.js")], check=True, cwd=str(ROOT))
    subprocess.run(["node", "--check", str(ROOT / "static" / "js" / "chat.js")], check=True, cwd=str(ROOT))


def test_chat_js_hooks_browser_view_events_through_the_screenshot_whitelist():
    chat = (ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    assert "import browserView from './browserView.js';" in chat
    assert "json.type === 'browser_view'" in chat
    assert "browserView.push(json, chatRenderer.safeToolScreenshotSrc)" in chat


def test_style_has_delimited_browser_view_block_at_end():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    marker = "/* === browser view === */"
    assert marker in css
    assert ".browser-view-panel" in css.split(marker, 1)[1]


_FAKE_DOM = r"""
class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.hidden = false;
    this._html = ''; this.textContent = ''; this.src = ''; this.checked = false; this.listeners = {};
    this.classList = { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)}, contains(c){return this._s.has(c)}, toggle(c,on){on?this._s.add(c):this._s.delete(c)} };
  }
  set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classList._s].join(' '); }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  contains(c) { return this.children.includes(c) || this.children.some(k => k.contains && k.contains(c)); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  querySelector(sel) { return this._q[sel] || null; }
  querySelectorAll() { return []; }
}
const parts = {};
for (const cls of ['.bv-auto-toggle', '.bv-img', '.bv-empty', '.bv-page-title', '.bv-url', '.bv-filmstrip', '.bv-live']) parts[cls] = new El('div');
globalThis.document = {
  body: new El('body'),
  createElement(tag) { const e = new El(tag); e._q = parts; return e; },
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.localStorage = { _m: {}, getItem(k) { return k in this._m ? this._m[k] : null; }, setItem(k, v) { this._m[k] = String(v); } };
const bv = (await import('./static/js/browserView.js')).default;
"""


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_push_renders_frame_and_escapes_url_and_title():
    evil_title = "<img src=x onerror=alert(1)> & \"quotes\""
    evil_url = "javascript:alert(1)<script>"
    out = _node(_FAKE_DOM + f"""
const f = bv.push({{ type: 'browser_view', tool: 'mcp__builtin_browser__browser_navigate',
  url: {json.dumps(evil_url)}, title: {json.dumps(evil_title)}, screenshot: 'data:image/jpeg;base64,{JPEG}' }},
  (raw) => /^data:image\\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\\s]+$/i.test(String(raw)) ? raw : '');
const panel = document.body.children[0];
console.log(JSON.stringify({{
  stored: !!f,
  open: bv.isOpen(),
  id: panel.id,
  cls: panel.className,
  imgSrc: parts['.bv-img'].src,
  imgHidden: parts['.bv-img'].hidden,
  titleText: parts['.bv-page-title'].textContent,
  urlText: parts['.bv-url'].textContent,
  film: parts['.bv-filmstrip'].innerHTML,
  frames: bv.frames().length,
}}));
""")
    assert out["stored"] is True
    assert out["open"] is True  # auto-open on the first frame of a turn
    assert out["id"] == "browser-view-panel"
    assert "file-viewer-panel" in out["cls"] and "browser-view-panel" in out["cls"]
    assert out["imgSrc"] == f"data:image/jpeg;base64,{JPEG}"
    assert out["imgHidden"] is False
    # url/title are set as text, never markup
    assert out["titleText"] == evil_title
    assert out["urlText"] == evil_url
    # filmstrip HTML is escaped: no raw tag/quotes from the title, no <a href>
    assert "<img src=x onerror" not in out["film"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in out["film"]
    assert "<a " not in out["film"]
    assert out["frames"] == 1


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_push_rejects_non_raster_screenshots_and_keeps_last_eight():
    out = _node(_FAKE_DOM + f"""
const ok = (raw) => /^data:image\\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\\s]+$/i.test(String(raw)) ? raw : '';
const rejected = [
  bv.push({{ screenshot: 'javascript:alert(1)' }}, ok),
  bv.push({{ screenshot: 'data:text/html;base64,PHNjcmlwdD4=' }}, ok),
  bv.push({{ screenshot: 'data:image/svg+xml;base64,PHN2Zz4=' }}, ok),
  bv.push(null, ok),
];
for (let i = 0; i < 10; i++) bv.push({{ url: 'https://e.test/' + i, title: 't' + i, screenshot: 'data:image/png;base64,{JPEG}' }}, ok);
const fr = bv.frames();
console.log(JSON.stringify({{ rejected: rejected.every(r => r === null), imgSrcAfterRejects: parts['.bv-img'].src.startsWith('data:image/'),
  n: fr.length, first: fr[0].url, last: fr[fr.length - 1].url, film: parts['.bv-filmstrip'].innerHTML, activeIdx: (parts['.bv-filmstrip'].innerHTML.match(/bv-thumb active/g) || []).length }}));
""")
    assert out["rejected"] is True
    assert out["n"] == 8
    assert out["first"] == "https://e.test/2" and out["last"] == "https://e.test/9"
    assert out["activeIdx"] == 1
    assert out["film"].count("data-bv=\"frame\"") == 8


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_auto_open_pref_and_live_dot():
    out = _node(_FAKE_DOM + f"""
localStorage.setItem('odysseus.browserView.auto', '0');
const ok = (raw) => raw;
bv.push({{ url: 'https://e.test/', title: 't', screenshot: 'data:image/jpeg;base64,{JPEG}' }}, ok);
const closedByPref = !bv.isOpen();
bv.open();
bv.setLive(true); const liveShown = !parts['.bv-live'].hidden;
bv.setLive(false); const liveHidden = parts['.bv-live'].hidden;
bv.setAutoOpen(true);
console.log(JSON.stringify({{ closedByPref, liveShown, liveHidden, pref: localStorage.getItem('odysseus.browserView.auto'), auto: bv.autoOpenEnabled(), cb: parts['.bv-auto-toggle'].checked }}));
""")
    assert out == {"closedByPref": True, "liveShown": True, "liveHidden": True, "pref": "1", "auto": True, "cb": True}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_live_means_a_browser_action_in_the_streaming_turn():
    """Seen live (ronda 6): the dot stayed on after the turn ended and even
    lit up in ANOTHER chat that never touched the browser, because "live"
    followed the chat-busy flag. It must follow frames of the streaming turn
    and go out at the end of the turn or when the user switches chat."""
    out = _node(_FAKE_DOM.replace("addEventListener() {},\n};", "addEventListener(t, fn) { (this.listeners = this.listeners || {}); (this.listeners[t] = this.listeners[t] || []).push(fn); },\n};") + f"""
const winListeners = {{}};
globalThis.addEventListener = (t, fn) => {{ (winListeners[t] = winListeners[t] || []).push(fn); }};
// the module registered its listeners at import time ⇒ re-import a fresh copy
const bv2 = (await import('./static/js/browserView.js?fresh=1')).default;
const fire = (t, detail) => (winListeners[t] || []).forEach(fn => fn({{ detail }}));
const fireDoc = (t) => ((document.listeners || {{}})[t] || []).forEach(fn => fn({{}}));
const ok = (raw) => raw;
const steps = [];
fire('odysseus:chat-busy-change', {{ active: true }});          // a turn starts, no browser yet
window.__odysseusChatBusy = true;
bv2.push({{ url: 'https://e.test/', title: 't', screenshot: 'data:image/jpeg;base64,{JPEG}' }}, ok);
steps.push(['frame while busy', !parts['.bv-live'].hidden]);
fire('odysseus:chat-busy-change', {{ active: false }});         // the turn ends
window.__odysseusChatBusy = false;
steps.push(['turn ended', !parts['.bv-live'].hidden]);
fire('odysseus:chat-busy-change', {{ active: true }});          // another turn that never browses
steps.push(['busy without a frame', !parts['.bv-live'].hidden]);
window.__odysseusChatBusy = true;
bv2.push({{ url: 'https://e.test/2', title: 't2', screenshot: 'data:image/jpeg;base64,{JPEG}' }}, ok);
steps.push(['frame again', !parts['.bv-live'].hidden]);
fireDoc('odysseus:session-switch');                              // user opens another chat
steps.push(['after chat switch', !parts['.bv-live'].hidden]);
console.log(JSON.stringify(Object.fromEntries(steps)));
""")
    assert out == {
        "frame while busy": True,
        "turn ended": False,
        "busy without a frame": False,
        "frame again": True,
        "after chat switch": False,
    }
