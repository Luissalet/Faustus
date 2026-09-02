"""Sub-agent board v3 (multi-agent orchestration with live control boards).

Node-based checks of the real agentHarnessUI.js / slashCommands.js code plus
source contracts on chat.js, sessions.js, chatRenderer.js and style.css:

- one card per worker: elapsed ticker from `started_at` (tick's `elapsed_s`
  wins over the wall clock; first-seen time as the fallback), stalled pill
  ("no activity Ns" / "loop"), tokens `in/out`, round, steer + supervisor lines;
- events that arrive while the parent chat is in the background are RETAINED
  (chat.js used to `continue` on `_isBg`) and repainted on return;
- "↻ Re-run…" is refused while the parent streams (a submit would have been a
  Stop of the whole delegation) and the delegation payload never survives a
  submit that did not go through;
- the persisted board (tool_events[i].subagents) is rebuilt with the richer
  fields (role, tokens, files, model, elapsed, supervisor, Re-run);
- sidebar worker rows get a Stop control + stalled mark, and an open running
  worker chat shows a banner instead of an empty chat.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = (_REPO / "static/js/agentHarnessUI.js").read_text(encoding="utf-8")
CHAT_JS = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
SESSIONS_JS = (_REPO / "static/js/sessions.js").read_text(encoding="utf-8")
SLASH_JS = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
RENDERER_JS = (_REPO / "static/js/chatRenderer.js").read_text(encoding="utf-8")
CSS = (_REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

# A minimal DOM: enough for _boardFor() / the cards / the 1 s ticker. innerHTML
# assignments are parsed into flat children (one per `class="…"`), so the
# selectors the module uses afterwards (`.subagent-elapsed`, `.subagent-pill`,
# `.agent-progress-toggle`…) resolve.
_DOM_STUB = r"""
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null; this.dataset = {}; this.style = {};
    this._html = ''; this.textContent = ''; this._cls = ''; this.disabled = false; this.title = ''; this.hidden = false;
    const self = this;
    this.classList = {
      contains(c) { return self._cls.split(/\s+/).includes(c); },
      add(c) { if (!this.contains(c)) self._cls = (self._cls + ' ' + c).trim(); },
      remove(c) { self._cls = self._cls.split(/\s+/).filter(x => x !== c).join(' '); },
      toggle(c, f) { const has = this.contains(c); const want = f === undefined ? !has : !!f; if (want && !has) this.add(c); if (!want && has) this.remove(c); return want; },
    };
  }
  get className() { return this._cls; }
  set className(v) { this._cls = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    this.children = [];
    const re = /<(\w+)([^>]*)class="([^"]*)"([^>]*)>/g;
    let m;
    while ((m = re.exec(this._html))) {
      const c = new El(m[1]); c._cls = m[3];
      const attrs = m[2] + ' ' + m[4];
      const ds = /data-sa="([^"]*)"/.exec(attrs); if (ds) c.dataset.sa = ds[1];
      const rr = /data-rerun-worker="/.test(attrs); if (rr) c.dataset.rerunWorker = '1';
      c.disabled = /\sdisabled(\s|>|$)/.test(attrs);
      const t = /title="([^"]*)"/.exec(attrs); if (t) c.title = t[1];
      this.appendChild(c);
    }
  }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(x => x !== this); this.parentNode = null; } }
  addEventListener() {}
  closest(sel) { let n = this; while (n) { if (n._matches && n._matches(sel)) return n; n = n.parentNode; } return null; }
  _matches(sel) {
    sel = sel.replace(/:last-of-type$/, '');
    const m = sel.match(/^\[data-sa="([^"]*)"\]$/);
    if (m) return this.dataset.sa === m[1];
    if (sel === '[data-rerun-worker]') return !!this.dataset.rerunWorker;
    return sel.split('.').filter(Boolean).every(c => this.classList.contains(c));
  }
  _all(sel, out = []) { for (const c of this.children) { if (c._matches(sel)) out.push(c); c._all(sel, out); } return out; }
  querySelector(sel) { return this._all(sel)[0] || null; }
  querySelectorAll(sel) { return this._all(sel); }
}
const chatBox = new El('div'); chatBox.id = 'chat-history';
const body = new El('body');
let domTouched = 0;
globalThis.document = {
  body,
  getElementById(id) { domTouched += 1; return id === 'chat-history' ? chatBox : null; },
  createElement(tag) { return new El(tag); },
  addEventListener() {},
  querySelectorAll(sel) { return chatBox._all(sel).concat(body._all(sel)); },
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.CSS = { escape: s => s };
const intervals = [];
globalThis.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
globalThis.clearInterval = () => {};
const toasts = [];
globalThis.uiModule = { showToast(m) { toasts.push(m); }, esc: s => String(s) };
"""


def _harness_module_source():
    src = (HARNESS_JS
           .replace("export function", "function")
           .replace("export async function", "async function")
           .replace("export const", "const")
           .replace("export default agentHarnessUI;", ""))
    return src


def _run_node(script):
    proc = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _ev(**sa):
    return json.dumps({"type": "tool_progress", "tool": "delegate_agents", "subagent": sa})


# ── source contracts ────────────────────────────────────────────────────────

def test_chat_js_keeps_subagent_events_in_the_background():
    block = CHAT_JS.split("} else if (json.type === 'tool_progress') {", 1)[1].split("} else if (json.type === 'tool_output')", 1)[0]
    sub = block.index("if (json.subagent) {")
    bg = block.index("if (_isBg) continue;")
    assert sub < bg, "the subagent branch must run BEFORE the background discard"
    assert "renderSubagentEvent(json, { sessionId: streamSessionId, background: _isBg })" in block
    resume = CHAT_JS.split("export async function resumeStream", 1)[1].split("export function checkBackgroundStream", 1)[0]
    assert "agentHarnessUI.renderSubagentEvent(json)" in resume     # reload mid-run: /resume replays the buffer
    bgs = CHAT_JS.split("export function checkBackgroundStream", 1)[1].split("function _markCompactPre", 1)[0]
    assert "agentHarnessUI.restoreSubagentBoard(sessionId)" in bgs
    assert bgs.index("restoreSubagentBoard(sessionId)") < bgs.index("box.appendChild(holder)"), "board before the spinner holder"


def test_chat_js_streaming_branch_never_keeps_a_delegation_payload():
    branch = CHAT_JS.split("    if (isStreaming) {\n      // A delegation payload", 1)[1].split("abortCurrentRequest(true)", 1)[0]
    assert "window.__odysseusDelegateTasks = null" in branch
    assert "if (_delegateRefused) return;" in branch, "a delegation while streaming must not turn into a Stop of the run"
    consume = CHAT_JS.split("fd.append('delegate_tasks'", 1)[1][:400]
    assert "window.__odysseusDelegateTasks = null;" in consume


def test_slash_delegate_tasks_refuses_while_streaming_and_drops_a_stale_payload():
    fn = SLASH_JS.split("export function delegateTasks(tasks, { parallel = true, review = false } = {})", 1)[1].split("function _boundWorkspacePath", 1)[0]
    assert fn.index("if (_delegationBlocked())") < fn.index("window.__odysseusDelegateTasks = payload")
    assert "_dropDelegatePayload(payload, { toast: true })" in fn
    assert "function _delegationBlocked()" in fn and "_isStreamingFn()" in fn and "cm.hasActiveStream(sid)" in fn
    assert "window.__odysseusDelegateTasks !== payload" in fn   # a newer delegation's payload is not ours to drop


def test_harness_ui_board_v3_contract():
    sa = HARNESS_JS.split("export function renderSubagentEvent", 1)[1].split("// ── Progress panel", 1)[0]
    for needle in ("data-stop-worker=", "data-steer-worker=", "data-rerun-worker=", "subagent-chat-link",
                   "/api/chat/subagent/steer/", "/api/chat/subagent/stop/", "wait for the delegation to finish",
                   "subagent-pill", "subagent-elapsed", "subagent-tokens", "subagent-round", "subagent-tail",
                   "subagent-steer", "subagent-supervisor", "no activity ", "'loop'"):
        assert needle in sa, needle
    init = HARNESS_JS.split("export function init(apiBase)", 1)[1]
    assert "['[data-steer-worker]', _steerWorker]" in init
    assert "restoreSubagentBoard, restoredSubagentBoardHtml" in HARNESS_JS
    rerun = HARNESS_JS.split("function _rerunWorker(", 1)[1].split("\n}\n", 1)[0]
    assert rerun.index("_parentStreaming()") < rerun.index("window.prompt("), "refuse before asking for a model"


def test_sessions_worker_rows_banner_and_activity_workers():
    sync = SESSIONS_JS.split("async function _syncActivityFromServer", 1)[1].split("\nlet _serverQueued", 1)[0]
    assert "_serverWorkers = " in sync and "data.workers" in sync
    assert "export function workerInfo(sessionId)" in SESSIONS_JS and "export async function stopWorker(sessionId)" in SESSIONS_JS
    assert "/api/chat/subagent/stop/" in SESSIONS_JS.split("export async function stopWorker", 1)[1][:600]
    row = SESSIONS_JS.split("function createSessionItem(s)", 1)[1].split("function _dateBucketLabel", 1)[0]
    assert "session-worker-stop" in row and "_applyWorkerRowState(div, s.id)" in row
    assert "(_serverRunIds[s.id] || workerInfo(s.id))" in row   # dropdown Stop run also for workers
    assert "if (!_serverRunIds[s.id] && workerInfo(s.id)) { await stopWorker(s.id); return; }" in row
    dots = SESSIONS_JS.split("function _updateResearchDots()", 1)[1].split("\n}\n", 1)[0]
    assert ".session-item-worker[data-session-id]" in dots and "_applyWorkerRowState(row" in dots
    state = SESSIONS_JS.split("function _applyWorkerRowState(row, sid)", 1)[1].split("\n}\n", 1)[0]
    assert "'worker-stalled', stalled" in state and "'worker-running', running" in state
    check = SESSIONS_JS.split("async function _checkServerStream(sessionId)", 1)[1].split("\n}\n", 1)[0]
    assert "if (await _showRunningWorkerBanner(sessionId)) return;" in check
    banner = SESSIONS_JS.split("async function _showRunningWorkerBanner(sessionId)", 1)[1].split("\n}\n", 1)[0]
    for needle in ("worker-chat-banner", "worker-chat-stop", "worker-chat-open-parent", "Open parent", "stopWorker(sessionId)", "selectSession(sessionId)", "is-stalled"):
        assert needle in banner, needle
    assert "  workerInfo,\n  stopWorker," in SESSIONS_JS


def test_renderer_uses_the_v3_restored_board_with_a_fallback():
    seg = RENDERER_JS.split("let saHtml = '';", 1)[1].split("node.innerHTML = `<div class=\"agent-thread-dot\">", 1)[0]
    assert "window.agentHarnessUI.restoredSubagentBoardHtml(ev.subagents)" in seg
    assert "if (!saHtml && Array.isArray(ev.subagents)" in seg    # v2 rows when agentHarnessUI is not loaded


def test_css_block_is_delimited_and_narrow_friendly():
    i = CSS.index("/* === subagent board v3 === */")
    block = CSS[i:]
    for sel in (".subagent-cards", ".subagent-card", ".subagent-pill.is-stalled", ".subagent-role-badge", ".subagent-tail",
                ".session-worker-stop", ".session-item-worker.worker-stalled", ".worker-chat-banner"):
        assert sel in block, sel
    assert re.search(r"@media \(max-width: 640px\) \{\s*\.subagent-cards \{ grid-template-columns: 1fr; \}", block)


# ── node: the real code ─────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_elapsed_ticker_from_started_at_then_tick_then_first_seen():
    script = (_DOM_STUB + _harness_module_source() + r"""
const I = _subagentInternals;
const now = Date.now();
// 1. started_at from the server: elapsed counts from it (no tick yet).
renderSubagentEvent(""" + _ev(id="w1", index=0, name="api", event="started", session_id="c1", started_at="__T__") + r""", { sessionId: 's1', background: true });
const [w1] = subagentBoardState('s1');
const e1 = I.elapsed(w1, now);
// 2. a tick carries its own elapsed_s: it wins over the wall clock (clock skew),
//    and keeps counting between ticks.
renderSubagentEvent(""" + _ev(id="w1", event="tick", elapsed_s=500, idle_s=1, round=2, tool_calls=3, stalled=False) + r""", { sessionId: 's1', background: true });
const e2 = I.elapsed(w1, now + 4000);
// 3. no started_at at all (old backend): first-seen time is the base.
renderSubagentEvent(""" + _ev(id="w2", index=1, name="ui", event="started", session_id="c2") + r""", { sessionId: 's1', background: true });
const w2 = subagentBoardState('s1')[1];
const e3 = I.elapsed(w2, w2.firstSeen + 12000);
// 4. done with started_at + ended_at: frozen to the server's numbers.
renderSubagentEvent(""" + _ev(id="w2", event="done", stop_reason="complete", tool_calls=5, mutations=["a.py"], duration_s=99, started_at=1000, ended_at=1075) + r""", { sessionId: 's1', background: true });
const e4 = I.elapsed(w2, w2.firstSeen + 999999);
const html = I.cardHtml(w1, { live: true, now });
console.log(JSON.stringify({ e1, e2, e3, e4, fmt: [I.fmtDur(5), I.fmtDur(75), I.fmtDur(3725)], html, status: [w1.status, w2.status] }));
""").replace('"__T__"', "(Date.now() / 1000 - 30)")
    out = _run_node(script)
    assert 29 <= out["e1"] <= 31.5
    assert 503.5 <= out["e2"] <= 504.5
    assert out["e3"] == 12
    assert out["e4"] == 75
    assert out["fmt"] == ["5s", "1m 15s", "1h 02m"]
    assert 'class="subagent-elapsed" data-started=' in out["html"]
    assert '<span class="subagent-round" title="round">r2</span>' in out["html"]
    assert out["status"] == ["running", "done"]


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_stalled_pill_tokens_round_and_lines():
    script = _DOM_STUB + _harness_module_source() + r"""
const I = _subagentInternals;
const S = (sa) => renderSubagentEvent({ subagent: sa }, { sessionId: 's1', background: true });
S({ id: 'w', index: 0, name: 'api', role: 'worker', event: 'started', session_id: 'c1', instruction: 'fix the api', files: ['src/api.py'], model: 'qwen3', max_rounds: 12 });
S({ id: 'w', event: 'tool', tool: 'bash', command: 'pytest -q', phase: 'start' });
S({ id: 'w', event: 'tool', tool: 'bash', phase: 'progress', elapsed_s: 7, tail: 'collecting…\n3 passed' });
const [w] = subagentBoardState('s1');
const inflight = I.cardHtml(w, { live: true });
S({ id: 'w', event: 'tick', elapsed_s: 140, idle_s: 134, round: 3, last_tool: 'bash', tool_calls: 4, input_tokens: 1234, output_tokens: 340, stalled: true, stall_reason: 'idle' });
const now = Date.now();
const stalled = I.pill(w, now);
const stalledLater = I.pill(w, now + 6000);
const html = I.cardHtml(w, { live: true, now });
S({ id: 'w', event: 'tick', elapsed_s: 150, idle_s: 0, round: 4, tool_calls: 5, stalled: true, stall_reason: 'loop' });
const loop = I.pill(w, Date.now());
S({ id: 'w', event: 'tool', tool: 'edit_file', ok: true, phase: 'done', output: 'ok' });
const afterTool = I.pill(w, Date.now());
S({ id: 'w', event: 'steer', text: 'use the fixtures in tests/conftest.py', source: 'user' });
S({ id: 'w', event: 'supervisor', action: 'nudge', reason: 'loop' });
S({ id: 'w', event: 'queued' });   // out of order on purpose: pill must follow the state
const queued = I.pill(w, Date.now());
S({ id: 'w', event: 'started' });
S({ id: 'w', event: 'done', stop_reason: 'rounds_exhausted', tool_calls: 9, failed_calls: 1, mutations: ['src/api.py'], duration_s: 200, input_tokens: 9000, output_tokens: 1200, rounds: 12 });
const done = I.cardHtml(w, { live: true });
console.log(JSON.stringify({ inflight, stalled, stalledLater, html, loop, afterTool, queued, done, tools: w.toolCalls }));
"""
    out = _run_node(script)
    assert "▶ bash (7s) pytest -q" in out["inflight"] and '<pre class="subagent-tail">collecting…\n3 passed</pre>' in out["inflight"]
    assert out["stalled"] == {"kind": "stalled", "text": "no activity 134s"}
    assert out["stalledLater"]["text"] == "no activity 140s"     # keeps counting between ticks
    assert '<span class="subagent-pill is-stalled">no activity 134s</span>' in out["html"]
    assert '<span class="subagent-tokens" title="tokens">1.2k in · 340 out</span>' in out["html"]
    assert 'r3/12' in out["html"] and '4 tools' in out["html"]
    assert 'class="subagent-role-badge is-worker">worker' in out["html"] and 'title="model">qwen3' in out["html"]
    assert 'owns <code class="subagent-file" title="src/api.py">api.py</code>' in out["html"]
    assert 'data-stop-worker="c1"' in out["html"] and 'data-steer-worker="c1"' in out["html"] and 'href="#c1"' in out["html"]
    assert out["loop"] == {"kind": "stalled", "text": "loop"}
    assert out["afterTool"] == {"kind": "running", "text": "running"}   # activity clears the stall
    assert out["queued"] == {"kind": "queued", "text": "queued"}
    done = out["done"]
    assert '<span class="subagent-pill is-partial">rounds_exhausted</span>' in done
    assert "9 tools (1 failed)" in done and "9.0k in · 1.2k out" in done and "1 file changed" in done
    assert '<div class="subagent-steer">→ steered: use the fixtures in tests/conftest.py</div>' in done
    assert '<div class="subagent-supervisor">supervisor: nudged — loop</div>' in done
    assert 'data-rerun-worker=' in done and 'data-stop-worker' not in done
    assert 'data-open-file="src/api.py" data-open-mode="diff"' in done
    assert out["tools"] == 9


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_background_events_are_retained_and_repainted_on_return():
    script = _DOM_STUB + _harness_module_source() + r"""
restoreProgress('s1');                    // the chat on screen (odysseus:session-switch)
domTouched = 0;
const S = (sa, bg) => renderSubagentEvent({ subagent: sa }, { sessionId: 's1', background: bg });
S({ id: 'a', index: 0, name: 'api', event: 'started', session_id: 'c1', instruction: 'x' }, true);
S({ id: 'b', index: 1, name: 'ui', event: 'started', session_id: 'c2', instruction: 'y' }, true);
S({ id: 'a', event: 'tick', elapsed_s: 30, idle_s: 30, stalled: true, stall_reason: 'idle', tool_calls: 2 }, true);
S({ id: 'b', event: 'done', stop_reason: 'complete', tool_calls: 3, mutations: [] }, true);
const touchedWhileBackground = domTouched;
const boardsBefore = chatBox.querySelectorAll('.subagent-board').length;
// The user comes back (chat.js checkBackgroundStream): repaint from state.
const painted = restoreSubagentBoard('s1');
const board = chatBox.querySelector('.subagent-board');
const cards = board ? board.querySelectorAll('.subagent-card') : [];
const count = board ? board.querySelector('.subagent-board-count').textContent : null;
const summary = board ? board.querySelector('.subagent-board-summary').textContent : null;
const a = cards.find(c => c.dataset.sa === 'a'); const b = cards.find(c => c.dataset.sa === 'b');
// A later live event (the user is on the chat now) updates the same card.
S({ id: 'a', event: 'tool', tool: 'bash', phase: 'start', command: 'ls' }, false);
const aAfter = board.querySelector('[data-sa="a"]');
// The 1 s ticker refreshes elapsed + pill of live cards without a re-render.
S({ id: 'a', event: 'tick', elapsed_s: 61, idle_s: 40, stalled: true, stall_reason: 'idle' }, false);
_subagentInternals.tick();
const elapsedText = aAfter.querySelector('.subagent-elapsed').textContent;
const pillText = aAfter.querySelector('.subagent-pill').textContent;
const unknown = restoreSubagentBoard('nope');
console.log(JSON.stringify({ touchedWhileBackground, boardsBefore, painted, cards: cards.length, count, summary,
  aCls: a && a.className, bCls: b && b.className, aHtmlHasStop: !!(a && a.innerHTML.includes('data-stop-worker="c1"')),
  aAfterLast: aAfter.querySelector('.subagent-last') ? aAfter.innerHTML.includes('▶ bash ls') : null,
  elapsedText, pillText, ticker: intervals.length > 0, unknown }));
"""
    out = _run_node(script)
    assert out["touchedWhileBackground"] == 0 and out["boardsBefore"] == 0   # nothing painted, nothing lost
    assert out["painted"] is True and out["cards"] == 2
    assert out["count"] == " 1/2" and out["summary"] == " · 1 running · 1 stalled"
    assert "is-running" in out["aCls"] and "is-live" in out["aCls"] and "is-stalled" in out["aCls"]
    assert "is-done" in out["bCls"] and "is-live" not in out["bCls"]
    assert out["aHtmlHasStop"] is True
    assert out["aAfterLast"] is True
    assert out["elapsedText"] == "1m 01s" and out["pillText"] == "no activity 40s"
    assert out["ticker"] is True
    assert out["unknown"] is False


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_rerun_refused_while_the_parent_streams():
    script = _DOM_STUB + _harness_module_source() + r"""
let streaming = true;
const delegated = []; let prompts = 0;
globalThis.chatModule = { hasActiveStream(sid) { return streaming && sid === 's1'; } };
globalThis.slashCommandsModule = { delegateTasks(tasks, opts) { delegated.push([tasks, opts]); } };
globalThis.prompt = () => { prompts += 1; return 'other-model'; };
restoreProgress('s1');                    // the chat on screen
const btn = new El('button');
btn.dataset.rerunWorker = JSON.stringify({ name: 'api', instruction: 'fix the api', files: ['a.py'], model: '' });
_subagentInternals.rerun(btn);
const refused = { delegated: delegated.length, prompts, toast: toasts[toasts.length - 1] || null };
// The card renders the button disabled with the hint while streaming…
const w = _subagentInternals.newWorker('w', Date.now());
_subagentInternals.apply(w, { event: 'done', name: 'api', instruction: 'fix the api', stop_reason: 'stopped', session_id: 'c1' }, Date.now());
const disabledHtml = _subagentInternals.cardHtml(w, { live: true, streaming: _subagentInternals.parentStreaming() });
// …and the ticker re-enables it when the parent is done.
const row = new El('div'); row.innerHTML = disabledHtml; chatBox.appendChild(row);
streaming = false;
_subagentInternals.tick();
const b = row.querySelector('[data-rerun-worker]');
_subagentInternals.rerun(btn);
console.log(JSON.stringify({ refused, disabledHtml, enabledAfter: { disabled: b.disabled, title: b.title }, delegated, prompts }));
"""
    out = _run_node(script)
    assert out["refused"] == {"delegated": 0, "prompts": 0, "toast": "Wait for the delegation to finish before re-running a worker"}
    assert 'data-rerun-worker=' in out["disabledHtml"] and ' disabled title="wait for the delegation to finish"' in out["disabledHtml"]
    assert out["enabledAfter"] == {"disabled": False, "title": "Delegate this task again (optionally with another model)"}
    assert out["prompts"] == 1 and out["delegated"] == [[[{"name": "api", "instruction": "fix the api", "files": ["a.py"], "model": "other-model"}], {"parallel": False}]]


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_restored_board_carries_the_persisted_fields():
    script = _DOM_STUB + _harness_module_source() + r"""
const html = restoredSubagentBoardHtml([
  { id: 'sa1', name: 'api', role: 'worker', session_id: 'c1', stop_reason: 'complete', tool_calls: 6, failed_calls: 0, mutations: ['src/api.py', 'tests/test_api.py'],
    duration_s: 44.2, input_tokens: 12000, output_tokens: 900, instruction: 'fix the api', model: 'qwen3', files: ['src/api.py'], started_at: 100, ended_at: 190, rounds: 4,
    steered: [{ text: 'hurry', source: 'supervisor' }], supervisor: [{ action: 'nudge', reason: 'loop' }], final_text: 'done, tests green' },
  { id: 'sa2', name: 'ui', role: 'worker', session_id: 'c2', stop_reason: 'stopped', tool_calls: 2, mutations: [], duration_s: 12, instruction: 'fix the ui' },
  { name: 'reviewer', role: 'reviewer', stop_reason: 'complete', tool_calls: 1, mutations: [] },
  // v2 record (old run): none of the new fields
  { id: 'old', name: 'legacy', stop_reason: 'error', error: 'model request failed', tool_calls: 0 },
]);
console.log(JSON.stringify({ html }));
"""
    html = _run_node(script)["html"]
    assert html.startswith('<div class="subagent-restored subagent-board-v3"><div class="subagent-restored-title">🤖 Sub-agents 2/4</div><div class="subagent-rows subagent-cards">')
    assert html.count('class="subagent-row subagent-card') == 4
    # richer fields
    assert '<span class="subagent-pill is-done">done</span>' in html
    assert '<span class="subagent-elapsed" data-started="100000" title="elapsed">1m 30s</span>' in html   # ended_at − started_at, not duration_s
    assert '12.0k in · 900 out' in html and 'r4' in html and '2 files changed' in html
    assert 'title="model">qwen3' in html and 'owns <code class="subagent-file" title="src/api.py">api.py</code>' in html
    assert 'data-open-file="tests/test_api.py" data-open-mode="diff"' in html
    assert '→ steered (supervisor): hurry' in html and 'supervisor: nudged — loop' in html
    assert 'done, tests green' in html
    # a successful worker has no Re-run; a stopped one re-runs with the persisted instruction; the
    # reviewer never does; the v2 record has no instruction to re-run with
    assert html.count('data-rerun-worker=') == 1
    assert 'data-rerun-worker="{&quot;name&quot;:&quot;ui&quot;,&quot;instruction&quot;:&quot;fix the ui&quot;' in html
    assert '<span class="subagent-pill is-stopped">stopped</span>' in html and '12s' in html
    assert 'class="subagent-role-badge is-reviewer">reviewer' in html and '🔍 reviewer' in html
    # a live-only control never appears on a restored board
    assert 'data-stop-worker' not in html and 'data-steer-worker' not in html
    assert '↗ Open chat' in html and 'href="#c1"' in html
    # v2 record still renders
    assert '<span class="subagent-pill is-failed">failed</span>' in html and '✗ model request failed' in html


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_slash_delegate_tasks_never_leaves_a_payload_behind():
    fn_src = "function delegateTasks" + SLASH_JS.split("export function delegateTasks", 1)[1].split("function _boundWorkspacePath", 1)[0]
    script = r"""
const toasts = []; const uiModule = { showToast(m) { toasts.push(m); } };
const sessionModule = { getCurrentSessionId() { return 's1'; } };
let streaming = false; const _isStreamingFn = () => streaming;
const input = { value: '', dispatchEvent() {} };
globalThis.window = globalThis; globalThis.Event = class { constructor(t) { this.type = t; } };
globalThis.document = { getElementById(id) { return id === 'message' ? input : null; } };
const submits = [];
let consume = false;
globalThis.chatModule = { hasActiveStream() { return false; }, async handleChatSubmit() { submits.push(input.value); if (consume) window.__odysseusDelegateTasks = null; } };
const tick = () => new Promise(r => setTimeout(r, 5));
""" + fn_src + r"""
// 1. streaming → refused up front, nothing set, nothing submitted
streaming = true;
delegateTasks([{ name: 'a', instruction: 'do a' }]);
await tick();
const s1 = { payload: window.__odysseusDelegateTasks || null, submits: submits.length, toast: toasts[toasts.length - 1] };
// 2. not streaming, the submit bails before consuming → payload dropped + toast
streaming = false;
delegateTasks([{ name: 'a', instruction: 'do a' }]);
await tick();
const s2 = { payload: window.__odysseusDelegateTasks || null, submits: submits.length, toast: toasts[toasts.length - 1], composer: input.value };
// 3. the submit consumed it → nothing to drop, no "not sent" toast
consume = true;
delegateTasks([{ name: 'b', instruction: 'do b' }], { parallel: false });
await tick();
const s3 = { payload: window.__odysseusDelegateTasks || null, submits: submits.length, toasts: toasts.length };
console.log(JSON.stringify({ s1, s2, s3 }));
"""
    out = _run_node(script)
    assert out["s1"] == {"payload": None, "submits": 0, "toast": "Wait for the current run to finish before delegating"}
    assert out["s2"]["payload"] is None and out["s2"]["submits"] == 1 and out["s2"]["toast"] == "The delegation was not sent"
    assert out["s2"]["composer"].startswith("🤖 1 sub-agent: do a")
    assert out["s3"] == {"payload": None, "submits": 2, "toasts": 2}
