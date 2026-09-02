"""Agent & automation settings form (static/js/agentSettings.js).

Node runs the real module against a fake schema + values:

- renderSchemaHtml: one control per field with the right type / bounds /
  options, legacy ids kept for the two pre-existing inputs, the raw key in
  monospace under the label, help text, restart hint only where flagged,
  is-changed marker when a value differs from its default, per-group Save,
  and HTML escaping of every schema/value string;
- parseFieldValue / valuesEqual / changedKeys / fieldMatches: the value
  contract the binder relies on;
- bindAgentSettings on a tiny DOM built from the rendered markup: editing
  marks the row dirty and enables the group's Save, Save POSTs only the
  changed keys of that group, the server's (clamped) answer is written back,
  reset restores the default, the filter hides non-matching rows and groups;
- source contracts: index.html hosts #agent-settings-root, admin.js loads the
  form, settings.js no longer binds the removed inputs, style.css carries the
  delimited block.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
MODULE_JS = (_REPO / "static/js/agentSettings.js").read_text(encoding="utf-8")
ADMIN_JS = (_REPO / "static/js/admin.js").read_text(encoding="utf-8")
SETTINGS_JS = (_REPO / "static/js/settings.js").read_text(encoding="utf-8")
REGISTRY_JS = (_REPO / "static/js/settings/registry.js").read_text(encoding="utf-8")
INDEX_HTML = (_REPO / "static/index.html").read_text(encoding="utf-8")
CSS = (_REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

# The module as a plain script: the one import becomes a stub, exports stay
# local bindings, the default export goes.
_MODULE_SRC = (MODULE_JS
               .replace("import { invalidateSettings } from './appConfig.js';",
                        "const invalidated = []; function invalidateSettings() { invalidated.push(1); }")
               .replace("export const", "const")
               .replace("export function", "function")
               .replace("export async function", "async function")
               .replace("export default agentSettings;", ""))

# A DOM just big enough for bindAgentSettings: the rendered markup is parsed
# into a tree; querySelector understands the compound selectors the module
# uses (tag / #id / .class / [attr] / [attr="v"], comma lists, no combinators).
_DOM_STUB = r"""
globalThis.setTimeout = () => 0;
const VOID = new Set(['input', 'br']);
class El {
  constructor(tag, attrs) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs || {}; this.children = []; this.parentNode = null;
    this.dataset = {}; this.disabled = 'disabled' in this.attrs; this.checked = 'checked' in this.attrs;
    this.value = this.attrs.value || ''; this.textContent = ''; this._html = '';
    for (const [k, v] of Object.entries(this.attrs)) {
      if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
    }
    const self = this;
    this.classList = {
      list() { return (self.attrs.class || '').split(/\s+/).filter(Boolean); },
      contains(c) { return this.list().includes(c); },
      add(c) { if (!this.contains(c)) self.attrs.class = [...this.list(), c].join(' '); },
      remove(c) { self.attrs.class = this.list().filter(x => x !== c).join(' '); },
      toggle(c, f) { const has = this.contains(c); const want = f === undefined ? !has : !!f; if (want && !has) this.add(c); if (!want && has) this.remove(c); return want; },
    };
    this._listeners = {};
  }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  get innerHTML() { return this._html; }
  set innerHTML(html) { this._html = html; this.children = []; parseInto(this, html); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  async dispatch(type, target) { for (const fn of (this._listeners[type] || [])) await fn({ target }); }
  matchesSimple(sel) {
    const m = sel.match(/^([a-z]*)((?:[#.][\w-]+|\[[^\]]+\])*)$/i);
    if (!m) throw new Error('unsupported selector ' + sel);
    if (m[1] && m[1].toUpperCase() !== this.tagName) return false;
    const parts = m[2].match(/[#.][\w-]+|\[[^\]]+\]/g) || [];
    for (const p of parts) {
      if (p[0] === '#') { if (this.id !== p.slice(1)) return false; }
      else if (p[0] === '.') { if (!this.classList.contains(p.slice(1))) return false; }
      else {
        const am = p.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
        if (!(am[1] in this.attrs)) return false;
        if (am[2] !== undefined && this.attrs[am[1]] !== am[2]) return false;
      }
    }
    return true;
  }
  matches(sel) { return sel.split(',').some(s => this.matchesSimple(s.trim())); }
  closest(sel) { let n = this; while (n) { if (n.matches(sel)) return n; n = n.parentNode; } return null; }
  querySelectorAll(sel) { const out = []; const walk = n => { for (const c of n.children) { if (c.matches(sel)) out.push(c); walk(c); } }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const UNESC = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
const unescape = s => s.replace(/&(amp|lt|gt|quot|#39);/g, m => UNESC[m]);
function parseAttrs(s) {
  const attrs = {}; const re = /([\w:-]+)(?:="([^"]*)")?/g; let m;
  while ((m = re.exec(s))) attrs[m[1]] = m[2] === undefined ? '' : unescape(m[2]);
  return attrs;
}
function parseInto(root, html) {
  const re = /<\/?([a-zA-Z][\w-]*)([^>]*)>/g; let m; let cur = root; let last = 0;
  while ((m = re.exec(html))) {
    const text = html.slice(last, m.index).trim(); if (text && cur !== root) cur.textContent += text; last = re.lastIndex;
    if (m[0][1] === '/') { if (cur !== root) cur = cur.parentNode; continue; }
    const el = new El(m[1], parseAttrs(m[2])); el.parentNode = cur; cur.children.push(el);
    if (!VOID.has(m[1].toLowerCase()) && !m[2].trim().endsWith('/')) cur = el;
  }
  for (const sel of root.querySelectorAll('select')) { const o = sel.children.find(c => 'selected' in c.attrs); if (o) sel.value = o.attrs.value; }
}
"""

_SCHEMA = {
    "groups": [
        {"id": "loop", "title": "Agent loop", "help": "How far <one> message may go.", "fields": [
            {"key": "agent_max_rounds", "label": "Max steps", "help": "Rounds per message.", "type": "int", "min": 1, "max": 200, "step": 1, "restart_hint": False},
            {"key": "agent_max_tool_calls", "label": "Tool call limit", "help": "0 = unlimited.", "type": "int", "min": 0, "max": 1000, "step": 1, "restart_hint": False},
            {"key": "agent_harness_checks", "label": "Harness", "help": "Checks after each turn.", "type": "bool", "restart_hint": False},
            {"key": "agent_local_temperature_cap", "label": "Temp cap", "help": "0 = never.", "type": "float", "min": 0, "max": 2, "step": 0.05, "restart_hint": True},
        ]},
        {"id": "browser", "title": "Browser", "help": "Applies on the next browser action.", "fields": [
            {"key": "browser_profile", "label": "Profile", "help": "persistent or isolated", "type": "select", "restart_hint": False,
             "options": [{"value": "isolated", "label": "isolated"}, {"value": "persistent", "label": "persistent"}]},
            {"key": "browser_cdp_endpoint", "label": "CDP <b>endpoint</b>", "help": "Start Chrome with --remote-debugging-port=9222 \"quoted\"", "type": "text", "placeholder": "http://127.0.0.1:9222", "restart_hint": False},
            {"key": "browser_token", "label": "Token", "help": "secret", "type": "secret", "restart_hint": False},
            {"key": "tool_path_extra_roots", "label": "Extra roots", "help": "comma-separated", "type": "list", "restart_hint": False},
        ]},
    ],
    "defaults": {
        "agent_max_rounds": 20, "agent_max_tool_calls": 0, "agent_harness_checks": True,
        "agent_local_temperature_cap": 0.4, "browser_profile": "persistent", "browser_cdp_endpoint": "",
        "browser_token": "", "tool_path_extra_roots": [],
    },
}
_VALUES = {
    "agent_max_rounds": 20, "agent_max_tool_calls": 50, "agent_harness_checks": False,
    "agent_local_temperature_cap": 0.4, "browser_profile": "isolated",
    "browser_cdp_endpoint": "<script>alert(1)</script>", "browser_token": "s3cret",
    "tool_path_extra_roots": ["/srv/a", "/srv/b"], "unrelated_key": "ignored",
}


def _run_node(script):
    proc = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _script(body):
    return (_DOM_STUB + _MODULE_SRC
            + f"\nconst SCHEMA = {json.dumps(_SCHEMA)};\nconst VALUES = {json.dumps(_VALUES)};\n" + body)


# ── rendering ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_render_controls_ids_and_escaping():
    html = _run_node(_script("console.log(JSON.stringify({ html: renderSchemaHtml(SCHEMA, VALUES) }));"))["html"]

    # legacy ids for the two pre-existing inputs, generated ids elsewhere
    assert 'id="set-agentMaxRounds"' in html and 'id="set-agentMaxTools"' in html
    assert 'id="agset-agent_harness_checks"' in html and 'for="agset-agent_harness_checks"' in html
    assert 'for="set-agentMaxRounds"' in html

    # control per type
    assert re.search(r'<input type="number"[^>]*data-agset-key="agent_max_rounds"[^>]*min="1"[^>]*max="200"[^>]*step="1"[^>]*value="20"', html)
    assert re.search(r'<input type="number"[^>]*data-agset-key="agent_local_temperature_cap"[^>]*step="0.05"', html)
    assert re.search(r'<label class="admin-switch agset-switch"><input type="checkbox" id="agset-agent_harness_checks" data-agset-key="agent_harness_checks"><span class="admin-slider"></span></label>', html)
    assert 'checked' not in re.search(r'<input type="checkbox"[^>]*agent_harness_checks[^>]*>', html).group(0)
    assert re.search(r'<select[^>]*data-agset-key="browser_profile"[^>]*>.*?<option value="isolated" selected>isolated</option><option value="persistent">persistent</option></select>', html, re.S)
    assert re.search(r'<input type="password"[^>]*data-agset-key="browser_token"[^>]*value="s3cret"', html)
    assert re.search(r'<input type="text"[^>]*agset-list[^>]*data-agset-key="tool_path_extra_roots"[^>]*value="/srv/a, /srv/b"', html)
    assert 'placeholder="http://127.0.0.1:9222"' in html

    # raw key under the label, help text, group Save + help
    assert '<code class="agset-key">agent_max_rounds</code>' in html
    assert 'class="admin-toggle-sub agset-help">Rounds per message.</div>' in html
    assert 'data-agset-save="loop"' in html and 'data-agset-save="browser"' in html
    assert html.count('class="admin-btn-add agset-save"') == 2
    assert 'Applies on the next browser action.' in html

    # restart hint only where flagged
    assert html.count('Restart needed to apply.') == 1
    assert re.search(r'data-agset-key="agent_local_temperature_cap"[^>]*>.*?Restart needed to apply', html, re.S)

    # is-changed marks values that differ from the default (50 ≠ 0, False ≠ True, isolated ≠ persistent, list)
    def row(key):
        return re.search(r'<div class="agset-field([^"]*)" data-agset-key="%s"' % key, html).group(1)
    assert row("agent_max_rounds") == "" and row("agent_local_temperature_cap") == ""
    assert row("agent_max_tool_calls") == " is-changed" and row("agent_harness_checks") == " is-changed"
    assert row("browser_profile") == " is-changed" and row("tool_path_extra_roots") == " is-changed"

    # escaping: labels, help, values and titles never carry raw markup
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "CDP &lt;b&gt;endpoint&lt;/b&gt;" in html
    assert "How far &lt;one&gt; message may go." in html
    assert "--remote-debugging-port=9222 &quot;quoted&quot;" in html
    assert 'title="Reset to default: 20"' in html and 'title="Reset to default: on"' in html
    assert 'title="Reset to default: empty"' in html
    assert "8 settings" in html and 'id="agset-search"' in html


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_value_helpers():
    out = _run_node(_script(r"""
const F = {}; for (const g of SCHEMA.groups) for (const f of g.fields) F[f.key] = f;
const r = {
  intClamp: parseFieldValue(F.agent_max_rounds, '999', 20),
  intBlank: parseFieldValue(F.agent_max_rounds, '', 20),
  intJunk: parseFieldValue(F.agent_max_rounds, 'abc', 20),
  floatLow: parseFieldValue(F.agent_local_temperature_cap, '-1', 0.4),
  list: parseFieldValue(F.tool_path_extra_roots, ' /a, , /b \n/c ', []),
  boolChecked: parseFieldValue(F.agent_harness_checks, true, false),
  boolStr: parseFieldValue(F.agent_harness_checks, 'off', true),
  text: parseFieldValue(F.browser_cdp_endpoint, '  http://x  ', ''),
  eqList: valuesEqual(F.tool_path_extra_roots, ['/a', '/b'], '/a, /b'),
  neList: valuesEqual(F.tool_path_extra_roots, ['/a'], ['/a', '/b']),
  eqNum: valuesEqual(F.agent_max_rounds, 20, '20'),
  eqBool: valuesEqual(F.agent_harness_checks, true, 'true'),
  changed: changedKeys(SCHEMA.groups[0].fields, { agent_max_rounds: 20, agent_max_tool_calls: 0, agent_harness_checks: true, agent_local_temperature_cap: 0.4 },
                                                 { agent_max_rounds: 20, agent_max_tool_calls: 7, agent_harness_checks: false }),
  matchKey: fieldMatches(F.browser_cdp_endpoint, 'cdp_endpoint'),
  matchHelp: fieldMatches(F.browser_cdp_endpoint, 'debugging chrome'),
  matchLabel: fieldMatches(F.agent_max_rounds, 'MAX steps'),
  noMatch: fieldMatches(F.agent_max_rounds, 'browser'),
  empty: fieldMatches(F.agent_max_rounds, '   '),
  ids: [controlId('agent_max_tool_calls'), controlId('agent_max_rounds'), controlId('browser_profile'), controlId('we ird')],
};
console.log(JSON.stringify(r));
"""))
    assert out["intClamp"] == 200 and out["intBlank"] == 20 and out["intJunk"] == 20
    assert out["floatLow"] == 0
    assert out["list"] == ["/a", "/b", "/c"]
    assert out["boolChecked"] is True and out["boolStr"] is False
    assert out["text"] == "http://x"
    assert out["eqList"] is True and out["neList"] is False and out["eqNum"] is True and out["eqBool"] is True
    assert out["changed"] == {"agent_max_tool_calls": 7, "agent_harness_checks": False}
    assert out["matchKey"] and out["matchHelp"] and out["matchLabel"] and not out["noMatch"] and out["empty"]
    assert out["ids"] == ["set-agentMaxTools", "set-agentMaxRounds", "agset-browser_profile", "agset-we_ird"]


# ── binder on the rendered markup ───────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_bind_dirty_save_only_changed_reset_and_filter():
    out = _run_node(_script(r"""
const posts = [];
async function post(payload) {
  posts.push(payload);
  // the server clamps rounds to 200 and echoes the full settings object
  const echo = { ...VALUES, ...payload };
  if ('agent_max_rounds' in payload) echo.agent_max_rounds = Math.min(payload.agent_max_rounds, 200);
  return echo;
}
const root = new El('div', { id: 'agent-settings-root' });
const ctl = bindAgentSettings(root, SCHEMA, VALUES, { post });
const q = s => root.querySelector(s);
const saveLoop = q('[data-agset-save="loop"]'), saveBrowser = q('[data-agset-save="browser"]');
const rounds = q('#set-agentMaxRounds'), harness = q('#agset-agent_harness_checks');
const r = { initialDisabled: [saveLoop.disabled, saveBrowser.disabled], initialText: saveLoop.textContent };

// 1. edit two fields of the loop group → rows dirty, Save enabled with a count; browser untouched
rounds.value = '150'; await root.dispatch('input', rounds);
harness.checked = true; await root.dispatch('change', harness);
r.afterEdit = { dirtyRounds: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-dirty'),
                dirtyHarness: q('.agset-field[data-agset-key="agent_harness_checks"]').classList.contains('is-dirty'),
                changedHarness: q('.agset-field[data-agset-key="agent_harness_checks"]').classList.contains('is-changed'),
                saveText: saveLoop.textContent, saveDisabled: saveLoop.disabled, browserDisabled: saveBrowser.disabled,
                unsaved: ctl.hasUnsavedChanges() };

// 2. a value typed back to what was loaded is not dirty
rounds.value = '20'; await root.dispatch('input', rounds);
r.backToLoaded = { dirty: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-dirty'), saveText: saveLoop.textContent };

// 3. save the loop group: only the changed keys go out, out-of-range is clamped client-side, server answer written back
rounds.value = '999'; await root.dispatch('input', rounds);
await root.dispatch('click', saveLoop);
r.afterSave = { posts: posts.map(p => Object.keys(p).sort()), payload: posts[0], roundsShown: rounds.value,
                dirtyRounds: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-dirty'),
                saveDisabled: saveLoop.disabled, msg: q('[data-agset-msg="loop"]').textContent, invalidated: invalidated.length };

// 4. reset to default: the row goes dirty again (loaded is 200 now, default 20) and Save re-enables
await root.dispatch('click', q('.agset-reset[data-agset-key="agent_max_rounds"]'));
r.afterReset = { value: rounds.value, dirty: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-dirty'),
                 changed: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-changed'), saveDisabled: saveLoop.disabled };

// 5. list + select in the browser group, saved as a list / string
q('#agset-tool_path_extra_roots').value = '/x, /y'; await root.dispatch('input', q('#agset-tool_path_extra_roots'));
q('#agset-browser_profile').value = 'persistent'; await root.dispatch('change', q('#agset-browser_profile'));
await root.dispatch('click', saveBrowser);
r.browserSave = posts[1];

// 6. a failing save reports and keeps the edit
const failing = bindAgentSettings(new El('div', {}), SCHEMA, VALUES, { post: async () => { throw new Error('nope'); } });
// (the failing binder's root is the parent of its controls)
// 7. filter: rows and empty groups hide, count updates
const search = q('#agset-search');
search.value = 'browser'; await root.dispatch('input', search); // bubbles from the search input
ctl.applyFilter();
r.filter = { loopHidden: q('[data-agset-group="loop"]').classList.contains('hidden'),
             browserHidden: q('[data-agset-group="browser"]').classList.contains('hidden'),
             roundsHidden: q('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('hidden'),
             cdpHidden: q('.agset-field[data-agset-key="browser_cdp_endpoint"]').classList.contains('hidden'),
             count: q('[data-agset-count]').textContent, emptyHidden: q('[data-agset-empty]').classList.contains('hidden') };
search.value = 'zzz-nothing'; ctl.applyFilter();
r.filterNone = { emptyHidden: q('[data-agset-empty]').classList.contains('hidden'), count: q('[data-agset-count]').textContent };
search.value = ''; ctl.applyFilter();
r.filterCleared = { count: q('[data-agset-count]').textContent, loopHidden: q('[data-agset-group="loop"]').classList.contains('hidden') };

// 8. refresh with fresh server values: untouched controls follow, an edit in progress is kept
harness.checked = false; await root.dispatch('change', harness);
ctl.refresh({ ...VALUES, agent_harness_checks: true, agent_max_tool_calls: 3 });
r.refresh = { harness: harness.checked, tools: q('#set-agentMaxTools').value, unsaved: ctl.hasUnsavedChanges() };
console.log(JSON.stringify(r));
"""))
    assert out["initialDisabled"] == [True, True] and out["initialText"] == "Save"
    e = out["afterEdit"]
    assert e["dirtyRounds"] and e["dirtyHarness"] and not e["changedHarness"]   # true == default
    assert e["saveText"] == "Save 2" and not e["saveDisabled"] and e["browserDisabled"] and e["unsaved"]
    assert out["backToLoaded"] == {"dirty": False, "saveText": "Save 1"}
    s = out["afterSave"]
    assert s["posts"] == [["agent_harness_checks", "agent_max_rounds"]]
    assert s["payload"] == {"agent_max_rounds": 200, "agent_harness_checks": True}   # clamped, only changed keys
    assert s["roundsShown"] == "200" and not s["dirtyRounds"] and s["saveDisabled"]
    assert s["msg"] == "Saved 2 settings" and s["invalidated"] == 0   # the test's post() is not the network one
    assert out["afterReset"] == {"value": "20", "dirty": True, "changed": False, "saveDisabled": False}
    assert out["browserSave"] == {"browser_profile": "persistent", "tool_path_extra_roots": ["/x", "/y"]}
    f = out["filter"]
    assert f["loopHidden"] and not f["browserHidden"] and f["roundsHidden"] and not f["cdpHidden"]
    # 3 rows carry "browser" in key/label/help; tool_path_extra_roots sits in the group but does not match
    assert f["count"] == "3 of 8 settings" and f["emptyHidden"]
    assert out["filterNone"] == {"emptyHidden": False, "count": "0 of 8 settings"}
    assert out["filterCleared"] == {"count": "8 settings", "loopHidden": False}
    assert out["refresh"] == {"harness": False, "tools": "3", "unsaved": True}


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_bind_reports_a_failed_save_and_keeps_the_edit():
    out = _run_node(_script(r"""
const root = new El('div', {});
bindAgentSettings(root, SCHEMA, VALUES, { post: async () => { throw new Error('HTTP 400: desktop_control_mode must be one of'); } });
const rounds = root.querySelector('#set-agentMaxRounds');
rounds.value = '33'; await root.dispatch('input', rounds);
await root.dispatch('click', root.querySelector('[data-agset-save="loop"]'));
const msg = root.querySelector('[data-agset-msg="loop"]');
console.log(JSON.stringify({ msg: msg.textContent, err: msg.classList.contains('is-error'), value: rounds.value,
  dirty: root.querySelector('.agset-field[data-agset-key="agent_max_rounds"]').classList.contains('is-dirty'),
  saveDisabled: root.querySelector('[data-agset-save="loop"]').disabled }));
"""))
    assert out["msg"].startswith("Failed to save: HTTP 400") and out["err"]
    assert out["value"] == "33" and out["dirty"] and not out["saveDisabled"]


# ── source contracts ────────────────────────────────────────────────────────

def test_index_html_hosts_the_form_and_dropped_the_two_inputs():
    panel = INDEX_HTML.split('data-settings-panel="tools"', 1)[1].split('data-settings-panel="system"', 1)[0]
    assert 'id="agent-settings-root"' in panel and 'id="agent-settings-card"' in panel
    assert 'Agent &amp; automation' in panel
    assert 'id="set-agentMaxTools"' not in INDEX_HTML and 'id="set-agentMaxRounds"' not in INDEX_HTML
    assert 'id="adm-builtin-tools-list"' in panel   # Built-in Tools card untouched


def test_admin_js_loads_the_form_with_the_tools_panel():
    assert "import { loadAgentSettings } from './agentSettings.js';" in ADMIN_JS
    refresh = ADMIN_JS.split("function refreshAll() {", 1)[1].split("\n}\n", 1)[0]
    assert "loadBuiltinTools();" in refresh and "loadAgentSettings()" in refresh


def test_settings_js_no_longer_binds_the_removed_inputs():
    assert "function initAgentSettings" not in SETTINGS_JS
    assert "initAgentSettings();" not in SETTINGS_JS
    assert "el('set-agentMaxTools')" not in SETTINGS_JS


def test_module_contract():
    assert "const SCHEMA_URL = '/api/agent/settings/schema';" in MODULE_JS
    assert "const SETTINGS_URL = '/api/auth/settings';" in MODULE_JS
    assert "agent_max_tool_calls: 'set-agentMaxTools'" in MODULE_JS and "agent_max_rounds: 'set-agentMaxRounds'" in MODULE_JS
    save = MODULE_JS.split("async function saveGroup(groupId) {", 1)[1].split("\n  }\n", 1)[0]
    assert "changedKeys(fields, loaded, currentFor(groupId))" in save, "only changed keys are posted"
    post = MODULE_JS.split("async function _postSettings(payload) {", 1)[1].split("\n}\n", 1)[0]
    assert "invalidateSettings();" in post and "finally" in post
    load = MODULE_JS.split("async function loadAgentSettings() {", 1)[1]
    assert "if (!_schemaPromise)" in load, "schema fetched once per page"
    assert "_controller.refresh(values)" in load
    assert "window.prompt(" not in MODULE_JS and "innerHTML = renderSchemaHtml" in MODULE_JS


def test_registry_keywords_route_the_nav_search_to_agent_tools():
    tools = REGISTRY_JS.split("id: 'tools',", 1)[1].split("}),", 1)[0]
    for kw in ("'automation'", "'browser'", "'desktop'", "'vision'", "'queue'"):
        assert kw in tools, kw
    assert "'provider'" not in tools and "'model'" not in tools   # would pollute other panels' searches


def test_css_block_is_delimited_and_at_the_end():
    i = CSS.index("/* === agent settings === */")
    block = CSS[i:]
    assert "/* === " not in block[len("/* === agent settings === */"):], "must be the last delimited block"
    for sel in (".agset-toolbar", ".agset-search", ".agset-group", ".agset-field", ".agset-key", ".agset-help",
                ".agset-hint", ".agset-reset", ".agset-field.is-dirty", ".agset-field.is-changed .agset-reset",
                ".agset-save:disabled", ".agset-group-msg.is-error", ".agset-field.hidden"):
        assert sel in block, sel
    assert re.search(r"@media \(max-width: 640px\) \{\s*\.agset-field \{ flex-direction: column;", block)
    assert "monospace" in block.split(".agset-key")[1].split("\n")[0]
