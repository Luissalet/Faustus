"""Settings → Local models, the page (static/js/localModels.js).

Node runs the real module against a tiny DOM: the pure renderers get the
payload routes/local_models_routes.py produces, and activate()/the click
handling get a fake fetch + EventSource so the pull re-attach, the confirm
dialog and the picker refresh are exercised without a browser. Source
contracts pin the wiring (nav item, panel, registry, settings.js, CSS block).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
MODULE_JS = (_REPO / "static/js/localModels.js").read_text(encoding="utf-8")
SETTINGS_JS = (_REPO / "static/js/settings.js").read_text(encoding="utf-8")
REGISTRY_JS = (_REPO / "static/js/settings/registry.js").read_text(encoding="utf-8")
INDEX_HTML = (_REPO / "static/index.html").read_text(encoding="utf-8")
CSS = (_REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

GIB = 1024 ** 3

_DATA = {
    "endpoint_id": "local-ollama",
    "endpoints": [
        {"id": "local-ollama", "name": "Ollama", "same_machine": True},
        {"id": "lan", "name": "Studio", "same_machine": False},
    ],
    "reachable": True,
    "vram": {"supported": True, "name": "NVIDIA GeForce RTX 4070 Ti", "total_bytes": 12 * GIB,
             "held_by_runner_bytes": 6 * GIB, "other_bytes": 1 * GIB, "reserve_bytes": 800 * 1024 ** 2,
             "budget_bytes": int(10.2 * GIB)},
    "loaded": [
        {"name": "qwen3.5:9b", "size": 8 * GIB, "size_vram": 6 * GIB, "size_cpu": 2 * GIB, "gpu_pct": 75,
         "expires_at": "2099-01-01T00:00:00Z", "context_length": 32768},
    ],
    "models": [
        {"name": "qwen3.5:9b", "size": int(6.6 * GIB), "digest": "aaa", "family": "qwen3", "parameter_size": "9B",
         "quantization": "Q4_K_M", "capabilities": {"vision": False, "tools": True, "thinking": True, "embedding": False},
         "context_length": 262144, "license": "Apache License", "fit": {"state": "fits", "note": "room to spare"},
         "loaded": True, "options": {"num_ctx": 32768, "keep_alive": "30m"}, "modified_at": "2026-08-01T10:00:00Z"},
        {"name": "qwen3.8:27b-q8_0", "size": 29_000_000_000, "digest": "bbb", "family": "qwen3", "parameter_size": "27B",
         "quantization": "Q8_0", "capabilities": {"vision": True, "tools": True, "thinking": False, "embedding": False},
         "context_length": 131072, "license": "", "fit": {"state": "over", "note": "It does not fit"},
         "loaded": False, "options": {}},
        {"name": "nomic-embed-text:latest", "size": 274_000_000, "digest": "ccc", "family": "nomic-bert",
         "capabilities": {"vision": False, "tools": False, "thinking": False, "embedding": True},
         "context_length": 8192, "fit": {"state": "fits"}, "loaded": False, "options": {}},
    ],
    "pulls": [
        {"id": "p1", "name": "gemma3:12b", "status": "pulling", "status_text": "pulling abc", "completed": 4 * GIB,
         "total": 8 * GIB, "percent": 50.0, "active": True, "started_at": 1},
    ],
}

_DISCOVER = {
    "vram": {"supported": True, "name": "RTX", "clean_budget_bytes": 11 * GIB, "total_bytes": 12 * GIB},
    "items": [
        {"name": "gemma3", "vendor": "Google", "blurb": "Gemma 3", "capabilities": ["vision"], "default_tag": "4b",
         "tags": [
             {"tag": "4b", "name": "gemma3:4b", "params": "4B", "size_bytes": int(3.3 * GIB), "fit": {"state": "fits"}, "installed": False},
             {"tag": "27b", "name": "gemma3:27b", "params": "27B", "size_bytes": 17 * GIB, "fit": {"state": "over"}, "installed": False},
         ]},
        {"name": "qwen3.5", "vendor": "Alibaba", "blurb": "Qwen", "capabilities": ["tools", "thinking"], "default_tag": "9b",
         "tags": [{"tag": "9b", "name": "qwen3.5:9b", "params": "9B", "size_bytes": int(6.6 * GIB), "fit": {"state": "fits"}, "installed": True}]},
    ],
}

# The module with its exports turned into plain declarations and its two
# imports replaced by globals, so node can run it as a script.
_MODULE_SRC = (MODULE_JS
               .replace("import uiModule from './ui.js';", "const uiModule = globalThis.uiModule;")
               .replace("import { invalidateSettings } from './appConfig.js';", "const invalidateSettings = () => { globalThis.invalidated = (globalThis.invalidated || 0) + 1; };")
               .replace("export function", "function")
               .replace("export default localModelsModule;", "globalThis.localModelsModule = localModelsModule;")
               .replace("export const", "const"))

_DOM_STUB = r"""
class El {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ''; this.children = []; this.parentNode = null; this.dataset = {}; this.style = {};
    this._html = ''; this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this._attrs = {};
    this._listeners = {}; this._cls = new Set();
    const self = this;
    this.classList = { add(c) { self._cls.add(c); }, remove(c) { self._cls.delete(c); }, toggle(c, f) { if (f) self._cls.add(c); else self._cls.delete(c); }, contains(c) { return self._cls.has(c); } };
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
  getAttribute(k) { return this._attrs[k] == null ? null : this._attrs[k]; }
  setAttribute(k, v) { this._attrs[k] = String(v); }
  hasAttribute(k) { return k in this._attrs; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  fire(type, ev) { (this._listeners[type] || []).forEach(fn => fn(ev)); }
  contains() { return true; }
  closest(sel) { if (sel === 'form') return this._form || null; return this; }
  querySelector() { return null; }
  focus() {}
}
const byId = {};
['lm-endpoint', 'lm-refresh', 'lm-vram', 'lm-status', 'lm-loaded', 'lm-installed', 'lm-installed-count', 'lm-pulls',
 'lm-pull-form', 'lm-pull-name', 'lm-pull-btn', 'lm-discover', 'lm-discover-note', 'lm-discover-q', 'lm-admin-hint']
  .forEach(id => { byId[id] = new El('div', id); });
const root = new El('div');
globalThis.document = {
  hidden: false,
  getElementById(id) { return byId[id] || null; },
  querySelector(sel) { return sel === '[data-settings-panel="local-models"]' ? root : null; },
};
globalThis.window = globalThis;
globalThis.CSS = { escape: s => s };
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
globalThis.setTimeout = (fn) => { fn(); return 1; };
globalThis.clearTimeout = () => {};
const toasts = [];
const confirms = [];
globalThis.uiModule = {
  showToast(m) { toasts.push(String(m)); },
  esc: s => String(s),
  async styledConfirm(msg, opts) { confirms.push({ msg, opts }); return globalThis.confirmAnswer; },
};
globalThis.confirmAnswer = true;
globalThis.confirm = () => { throw new Error('window.confirm must never be used'); };
const fetches = [];
globalThis.fetch = async (url, opts = {}) => {
  fetches.push({ url, method: (opts.method || 'GET'), body: opts.body ? JSON.parse(opts.body) : null });
  const respond = (obj, ok = true, status = 200) => ({ ok, status, async json() { return obj; } });
  if (url.startsWith('/api/local-models/discover')) return respond(globalThis.DISCOVER);
  if (url === '/api/local-models/pull?stream=false') return respond({ ok: true, created: true, pull: { id: 'p2', name: opts.body ? JSON.parse(opts.body).name : '', status: 'queued', active: true, percent: 0 } });
  if (url.startsWith('/api/local-models/pulls/') && (opts.method || 'GET') === 'DELETE') return respond({ ok: true });
  if (url.startsWith('/api/local-models/') && url.includes('/options')) return respond({ ok: true, options: { num_ctx: 8192 } });
  if (url.startsWith('/api/local-models/load')) return respond({ ok: true, via: '/api/generate', keep_alive: '5m' });
  if (url.startsWith('/api/local-models/unload')) return respond({ ok: true });
  if (url.startsWith('/api/local-models/') && (opts.method || 'GET') === 'DELETE') return respond({ ok: true });
  if (url === '/api/auth/settings') return respond({});
  if (url.startsWith('/api/local-models')) return respond(globalThis.DATA);
  return respond({}, false, 404);
};
const sources = [];
class EventSource {
  constructor(url) { this.url = url; this.closed = false; this._l = {}; sources.push(this); }
  addEventListener(t, fn) { (this._l[t] = this._l[t] || []).push(fn); }
  close() { this.closed = true; }
  emit(snap) { this.onmessage && this.onmessage({ data: JSON.stringify(snap) }); }
  end() { (this._l.end || []).forEach(fn => fn({})); }
}
globalThis.EventSource = EventSource;
const pickerRefreshes = [];
globalThis.modelsModule = { refreshModels(force) { pickerRefreshes.push(force); return Promise.resolve(); } };
globalThis.sessionModule = { updateModelPicker() {} };
const tick = () => new Promise(r => setImmediate(r));
const flush = async () => { for (let i = 0; i < 6; i++) await tick(); };
function fakeBtn(attrs) { const b = new El('button'); Object.entries(attrs).forEach(([k, v]) => b.setAttribute(k, v)); return b; }
function click(attrs) { const btn = fakeBtn(attrs); root.fire('click', { target: { closest: () => btn }, preventDefault() {} }); return btn; }
"""


def _run(script: str) -> dict:
    src = _DOM_STUB + f"\nglobalThis.DATA = {json.dumps(_DATA)};\nglobalThis.DISCOVER = {json.dumps(_DISCOVER)};\n" + _MODULE_SRC + "\n" + script
    proc = subprocess.run(["node", "--input-type=module"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── source contracts ────────────────────────────────────────────────────────

def test_every_touched_js_file_parses():
    for rel in ("static/js/localModels.js", "static/js/settings.js", "static/js/settings/registry.js"):
        proc = subprocess.run(["node", "--check", str(_REPO / rel)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{rel}: {proc.stderr}"


def test_nav_item_sits_right_after_added_models_and_the_panel_exists():
    added = INDEX_HTML.index('data-settings-tab="added-models"')
    local = INDEX_HTML.index('data-settings-tab="local-models"')
    ai = INDEX_HTML.index('data-settings-tab="ai"')
    assert added < local < ai
    nav = INDEX_HTML[local:local + 600]
    assert "<span>Local models</span>" in nav
    assert 'class="settings-nav-item" data-settings-tab="local-models"' in INDEX_HTML, "visible to every user, not admin-only"
    assert 'data-settings-panel="local-models"' in INDEX_HTML
    panel = INDEX_HTML.split('data-settings-panel="local-models"', 1)[1].split("<!-- ═══ TOOLS TAB", 1)[0]
    for needle in ('id="lm-endpoint"', 'id="lm-vram"', 'id="lm-loaded"', 'id="lm-installed"', 'id="lm-pull-form"',
                   'id="lm-pull-name"', 'id="lm-pulls"', 'id="lm-discover-q"', 'id="lm-discover"', 'class="admin-card'):
        assert needle in panel, needle


def test_registry_and_settings_wiring():
    entry = REGISTRY_JS.split("id: 'local-models',", 1)[1].split("}),", 1)[0]
    assert "label: 'Local models'" in entry and "group: 'models'" in entry
    assert "controller: 'admin'" not in entry          # settings.js activates it, for every user
    assert "'ollama'" in entry and "'vram'" in entry
    assert "'provider'" not in entry and "'model'" not in entry   # would pollute other panels' searches
    order = [m for m in ("id: 'added-models'", "id: 'local-models'", "id: 'ai'") ]
    assert REGISTRY_JS.index(order[0]) < REGISTRY_JS.index(order[1]) < REGISTRY_JS.index(order[2])
    assert "import localModelsModule from './localModels.js';" in SETTINGS_JS
    act = SETTINGS_JS.split("function onSettingsPanelActivated(tab)", 1)[1].split("\n}\n", 1)[0]
    assert "if (tab === 'local-models') localModelsModule.activate();" in act
    assert "else localModelsModule.deactivate();" in act
    close = SETTINGS_JS.split("export function close()", 1)[1].split("\n}\n", 1)[0]
    assert "localModelsModule.deactivate()" in close


def test_no_native_dialogs_and_the_app_confirm_is_used():
    assert "window.confirm(" not in MODULE_JS and "window.prompt(" not in MODULE_JS
    assert "styledConfirm(" in MODULE_JS
    delete = MODULE_JS.split("case 'delete': {", 1)[1].split("break;", 1)[0]
    assert "_confirm(" in delete and "if (!ok) return;" in delete


def test_css_block_is_delimited_and_at_the_end():
    i = CSS.index("/* === local models === */")
    block = CSS[i:]
    assert "/* === " not in block[len("/* === local models === */"):], "appended as the last delimited block"
    for sel in (".lm-vram-bar", ".lm-fit-tight", ".lm-fit-over", ".lm-row", ".lm-options-form", ".lm-pull-bar",
                ".lm-pull-indeterminate", ".lm-tag", ".lm-cap-vision", "@media (max-width: 720px)"):
        assert sel in block, sel


# ── the renderers under node ────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_installed_table_renders_fit_caps_ctx_and_admin_actions():
    out = _run("""
      const admin = renderInstalledHtml(DATA.models, { isAdmin: true, endpointId: 'local-ollama', canSetDefault: true });
      const user = renderInstalledHtml(DATA.models, { isAdmin: false, endpointId: 'local-ollama', canSetDefault: true });
      const noDefault = renderInstalledHtml(DATA.models, { isAdmin: true, canSetDefault: false });
      const withForm = renderInstalledHtml(DATA.models, { isAdmin: true, optionsOpen: 'qwen3.5:9b' });
      console.log(JSON.stringify({ admin, user, noDefault, withForm, empty: renderInstalledHtml([], {}) }));
    """)
    admin = out["admin"]
    assert 'lm-fit lm-fit-fits' in admin and '6.6 GB · fits' in admin
    assert 'lm-fit lm-fit-over' in admin and '27.0 GB · no fit' in admin
    assert 'title="It does not fit"' in admin
    assert 'lm-cap-vision' in admin and 'lm-cap-tools' in admin and 'lm-cap-thinking' in admin and 'lm-cap-embedding' in admin
    assert '>256k<' in admin and '>128k<' in admin and '>8k<' in admin
    assert 'Q4_K_M · 9B' in admin and 'Q8_0 · 27B' in admin
    assert 'lm-pill-loaded' in admin and 'lm-loaded-now' in admin
    assert 'ctx 32k · keep 30m' in admin           # saved options summary
    assert 'Apache License' in admin and 'qwen3' in admin
    # actions: loaded → Unload, not loaded → Load; Set default not for embeddings; Options + Delete for all
    assert admin.count('data-lm-action="unload"') == 1 and admin.count('data-lm-action="load"') == 2
    assert admin.count('data-lm-action="default"') == 2
    assert admin.count('data-lm-action="options"') == 3 and admin.count('data-lm-action="delete"') == 3
    assert 'data-lm-embedding="1"' in admin
    # read-only for everyone else
    assert 'data-lm-action=' not in out["user"]
    assert 'lm-fit-over' in out["user"]              # …but the facts are all there
    assert 'data-lm-action="default"' not in out["noDefault"]
    form = out["withForm"]
    assert 'data-lm-options-form="qwen3.5:9b"' in form and 'name="num_ctx"' in form and 'value="32768"' in form
    assert 'value="30m"' in form and 'model max 256k' in form
    assert 'data-lm-action="save-options"' in form and 'data-lm-action="close-options"' in form
    assert form.count('<form class="lm-options-form"') == 1      # only the open row carries the form
    assert 'admin-empty' in out["empty"] and 'pull one' in out["empty"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_loaded_vram_pulls_and_discover_render():
    out = _run("""
      console.log(JSON.stringify({
        loaded: renderLoadedHtml(DATA.loaded, { isAdmin: true }),
        loadedUser: renderLoadedHtml(DATA.loaded, { isAdmin: false }),
        none: renderLoadedHtml([], {}),
        vram: renderVramHtml(DATA.vram, DATA.loaded),
        noVram: renderVramHtml({ supported: false, reason: 'no nvidia-smi' }),
        pulls: renderPullsHtml(DATA.pulls.concat([
          { id: 'd', name: 'a:1', status: 'done', active: false, percent: 100 },
          { id: 'e', name: 'b:2', status: 'error', active: false, error: 'file does not exist' },
          { id: 'q', name: 'c:3', status: 'pulling', status_text: 'pulling manifest', active: true, total: 0, completed: 0 },
        ]), { isAdmin: true }),
        pullsUser: renderPullsHtml(DATA.pulls, { isAdmin: false }),
        disc: renderDiscoverHtml(DISCOVER.items, { isAdmin: true }),
        discUser: renderDiscoverHtml(DISCOVER.items, { isAdmin: false }),
        discEmpty: renderDiscoverHtml([], { q: 'zzz' }),
        eps: renderEndpointOptionsHtml(DATA.endpoints, 'lan'),
        fmt: [fmtGb(6.6 * 1073741824), fmtGb(274000000), fmtGb(0), fmtCtx(262144), fmtCtx(900), untilText('2099-01-01T00:00:00Z'), untilText('2000-01-01T00:00:00Z')],
      }));
    """)
    loaded = out["loaded"]
    assert 'qwen3.5:9b' in loaded and '8.0 GB resident · 6.0 GB VRAM' in loaded
    assert 'lm-split-spill' in loaded and '75% GPU · 25% CPU' in loaded
    assert 'ctx 32k' in loaded and 'kept loaded' in loaded
    assert 'data-lm-action="unload"' in loaded and 'data-lm-action=' not in out["loadedUser"]
    assert 'Nothing is loaded' in out["none"]
    vram = out["vram"]
    assert 'NVIDIA GeForce RTX 4070 Ti' in vram and '7.0 GB of 12.0 GB used · 5.0 GB free' in vram
    assert 'lm-vram-models" style="width:50.0%"' in vram and 'lm-vram-other" style="width:8.3%"' in vram
    assert 'budget 10.2 GB' in vram and 'reserve 800 MB' in vram
    assert 'No VRAM reading' in out["noVram"] and 'no nvidia-smi' in out["noVram"]
    pulls = out["pulls"]
    assert 'gemma3:12b' in pulls and 'pulling abc · 4.0 GB / 8.0 GB' in pulls and 'aria-valuenow="50"' in pulls
    assert 'data-lm-action="cancel-pull" data-lm-pull="p1"' in pulls
    assert 'lm-pull-done' in pulls and 'lm-pull-error' in pulls and 'failed: file does not exist' in pulls
    assert 'lm-pull-indeterminate' in pulls          # no total yet → animated bar
    assert 'data-lm-action="dismiss-pull"' in pulls
    assert 'cancel-pull' not in out["pullsUser"]
    disc = out["disc"]
    assert 'data-lm-disc="gemma3"' in disc and 'Google' in disc and 'lm-cap-vision' in disc
    assert 'lm-tag-default' in disc and '3.3 GB · fits' in disc and '17.0 GB · no fit' in disc
    assert 'data-lm-action="pull" data-lm-name="gemma3:27b"' in disc
    assert 'lm-tag-installed' in disc and 'lm-pill-installed' in disc
    assert 'data-lm-action="pull" data-lm-name="qwen3.5:9b"' not in disc     # installed → no Pull
    assert 'data-lm-action="pull"' not in out["discUser"] and 'no fit' in out["discUser"]
    assert 'zzz' in out["discEmpty"] and 'exact name' in out["discEmpty"]
    assert out["eps"].count('<option') == 2 and 'value="lan" selected' in out["eps"] and 'this machine' in out["eps"]
    assert out["fmt"] == ['6.6 GB', '261 MB', '—', '256k', '900', 'kept loaded', 'unloading']


# ── behaviour: activate, re-attach, actions ─────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_activate_loads_the_page_and_reattaches_to_a_running_pull():
    out = _run("""
      globalThis._isAdmin = true;
      localModelsModule.activate();
      await flush();
      const firstFetch = fetches[0].url;
      const attached = sources.map(s => s.url);
      // the pull finishes while the page is open → picker refresh + toast
      sources[0].emit({ id: 'p1', name: 'gemma3:12b', status: 'done', active: false, percent: 100, version: 9 });
      sources[0].end();
      await flush();
      console.log(JSON.stringify({
        firstFetch, attached,
        discoverFetched: fetches.some(f => f.url.startsWith('/api/local-models/discover?q=')),
        installed: byId['lm-installed'].innerHTML.includes('qwen3.8:27b-q8_0'),
        vram: byId['lm-vram'].innerHTML.includes('RTX 4070'),
        pullsHidden: byId['lm-pulls'].hidden,
        pullsHtml: byId['lm-pulls'].innerHTML,
        note: byId['lm-discover-note'].textContent,
        toasts, pickerRefreshes,
        closed: sources[0].closed,
        active: localModelsModule.isActive(),
        pullDisabled: byId['lm-pull-btn'].disabled,
      }));
    """)
    assert out["firstFetch"] == "/api/local-models"
    assert out["attached"] == ["/api/local-models/pulls/p1/events"]
    assert out["discoverFetched"] and out["installed"] and out["vram"]
    assert out["pullsHidden"] is False and "lm-pull-done" in out["pullsHtml"]
    assert "11.0 GB usable of 12.0 GB" in out["note"]
    assert "Pulled gemma3:12b" in out["toasts"]
    assert out["pickerRefreshes"] == [True]
    assert out["closed"] is True and out["active"] is True
    assert out["pullDisabled"] is False


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_actions_go_through_the_api_and_the_app_confirm():
    out = _run("""
      globalThis._isAdmin = true;
      localModelsModule.activate();
      await flush();
      fetches.length = 0;
      // Delete: refused → nothing sent; accepted → DELETE with the endpoint
      globalThis.confirmAnswer = false;
      click({ 'data-lm-action': 'delete', 'data-lm-name': 'qwen3.8:27b-q8_0' });
      await flush();
      const afterRefused = fetches.map(f => f.method + ' ' + f.url);
      globalThis.confirmAnswer = true;
      click({ 'data-lm-action': 'delete', 'data-lm-name': 'hf.co/user/repo:Q4' });
      await flush();
      const deleteCall = fetches.find(f => f.method === 'DELETE');
      // Load / unload / set default / pull / cancel
      click({ 'data-lm-action': 'load', 'data-lm-name': 'nomic-embed-text:latest', 'data-lm-embedding': '1' });
      click({ 'data-lm-action': 'unload', 'data-lm-name': 'qwen3.5:9b' });
      click({ 'data-lm-action': 'default', 'data-lm-name': 'qwen3.5:9b' });
      click({ 'data-lm-action': 'pull', 'data-lm-name': 'gemma3:4b' });
      click({ 'data-lm-action': 'cancel-pull', 'data-lm-pull': 'p1' });
      await flush();
      // Options: open the form, then save through the form's own button
      click({ 'data-lm-action': 'options', 'data-lm-name': 'qwen3.5:9b' });
      const formOpen = byId['lm-installed'].innerHTML.includes('data-lm-options-form="qwen3.5:9b"');
      const form = new El('form');
      form.setAttribute('data-lm-options-form', 'qwen3.5:9b');
      const save = fakeBtn({ 'data-lm-action': 'save-options', 'data-lm-name': 'qwen3.5:9b' });
      save._form = form;
      form.querySelector = sel => sel.includes('save-options') ? save : { value: sel.includes('num_ctx') ? '8192' : '' };
      root.fire('submit', { target: form, preventDefault() {} });
      await flush();
      // A bad name never reaches the server
      byId['lm-pull-name'].value = 'rm -rf /';
      root.fire('submit', { target: byId['lm-pull-form'], preventDefault() {} });
      await flush();
      console.log(JSON.stringify({
        afterRefused, confirms: confirms.map(c => [c.msg, c.opts.confirmText, c.opts.danger]),
        deleteCall,
        calls: fetches.map(f => [f.method, f.url, f.body]),
        formOpen,
        toasts,
        invalidated: globalThis.invalidated || 0,
        attached: sources.map(s => s.url),
      }));
    """)
    assert out["afterRefused"] == []
    assert out["confirms"][0][0].startswith("Delete qwen3.8:27b-q8_0") and out["confirms"][0][1:] == ["Delete", True]
    assert out["deleteCall"]["url"] == "/api/local-models/hf.co/user/repo%3AQ4?endpoint_id=local-ollama"
    calls = out["calls"]
    assert ["POST", "/api/local-models/load", {"endpoint_id": "local-ollama", "name": "nomic-embed-text:latest", "embedding": True}] in calls
    assert ["POST", "/api/local-models/unload", {"endpoint_id": "local-ollama", "name": "qwen3.5:9b", "embedding": False}] in calls
    assert ["POST", "/api/auth/settings", {"default_endpoint_id": "local-ollama", "default_model": "qwen3.5:9b"}] in calls
    assert ["POST", "/api/local-models/pull?stream=false", {"endpoint_id": "local-ollama", "name": "gemma3:4b"}] in calls
    assert ["DELETE", "/api/local-models/pulls/p1", None] in calls
    assert out["formOpen"] is True
    put = next(c for c in calls if c[0] == "PUT")
    assert put[1] == "/api/local-models/qwen3.5%3A9b/options?endpoint_id=local-ollama"
    assert put[2] == {"options": {"num_ctx": "8192", "num_gpu": "", "keep_alive": ""}}
    assert not any("rm -rf" in json.dumps(c) for c in calls)
    assert any("does not look like an Ollama model name" in t for t in out["toasts"])
    assert "Pulling gemma3:4b…" in out["toasts"] and "Pull cancelled" in out["toasts"]
    assert "qwen3.5:9b is now the default chat model" in out["toasts"]
    assert "Saved options for qwen3.5:9b" in out["toasts"]
    assert out["invalidated"] >= 1
    assert "/api/local-models/pulls/p2/events" in out["attached"]     # the new pull is followed live


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_non_admins_get_a_read_only_page():
    out = _run("""
      globalThis._isAdmin = false;
      localModelsModule.activate();
      await flush();
      console.log(JSON.stringify({
        installed: byId['lm-installed'].innerHTML,
        loaded: byId['lm-loaded'].innerHTML,
        pullDisabled: byId['lm-pull-btn'].disabled,
        hint: byId['lm-admin-hint'].hidden,
        discover: byId['lm-discover'].innerHTML,
      }));
    """)
    assert "data-lm-action=" not in out["installed"] and "data-lm-action=" not in out["loaded"]
    assert "lm-fit-over" in out["installed"]
    assert out["pullDisabled"] is True and out["hint"] is False
    assert 'data-lm-action="pull"' not in out["discover"]
