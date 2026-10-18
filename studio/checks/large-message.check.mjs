// CPU DOM witnesses for the real transcript functions. No app, API or browser.
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import ts from 'typescript';
import { build, stop } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const transcript = 'studio/src/screens/studio/Transcript.tsx';
const scratch = mkdtempSync(join(tmpdir(), 'fs-large-message-'));
const require = createRequire(join(root, 'package.json'));
const window = new Window({ url: 'http://127.0.0.1/' });
globalThis.window = window; globalThis.document = window.document;
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let requests = 0;
globalThis.fetch = () => { requests++; throw new Error('No API in CPU check'); };
const names = new Set(['CopyButton', 'Editor', 'SectionEditor', 'MessagePager', 'LargeMessage', 'Said', 'UserTurn']);
async function product(source, name) {
  const tree = ts.createSourceFile(transcript, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const functions = tree.statements.filter(n => ts.isFunctionDeclaration(n) && names.has(n.name?.text));
  const contents = `import React,{Fragment,useState,useEffect,useRef,useMemo} from 'react';
    import {createRoot} from 'react-dom/client';
    import {Copy,Check,ChevronLeft,ChevronRight,RefreshCw,Pencil,Trash2,FileText} from 'lucide-react';
    import {Button} from './studio/src/components/Button';
    import {IconButton} from './studio/src/components/IconButton';
    import {t,setLang} from './studio/src/i18n';
    import {splitMentions} from './studio/src/lib/mentions';
    import {writeClipboardText} from './studio/src/lib/clipboard-write';
    import {IssueChip,renderIssueSegments} from './studio/src/screens/board/IssueChips';
    import {attachmentUrl,isImage} from './studio/src/adapters/composer';
    import * as helpers from './studio/src/lib/large-message';
    const {LARGE_MESSAGE_THRESHOLD,completeMessageEdit,findInMessage,messageContext,messageSections,sectionAt,startMessageEdit,textareaChange,textareaOffset,updateMessageEdit}=helpers;
    const Link=({to,children,...props})=>React.createElement('a',{href:to,...props},children);
    const AgentMessageTurn=()=>React.createElement('div',null,'agent message');
    ${functions.map(n => n.getFullText(tree)).join('\n')}
    export {React,createRoot,helpers,setLang,UserTurn,Editor};`;
  const file = join(scratch, name + '.cjs');
  await build({ stdin: { contents, loader: 'tsx', resolveDir: root }, bundle: true,
    platform: 'node', format: 'cjs', outfile: file, logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"development"' },
    plugins: [{ name: 'native-react-for-act-cleanup', setup(builder) {
      builder.onResolve({ filter: /^(react|react-dom)(\/.*)?$/ }, args => ({ path: require.resolve(args.path), external: true }));
    } }] });
  return require(file);
}
const source = readFileSync(join(root, transcript), 'utf8');
const p = await product(source, 'after');
const h = p.helpers;
const exact = process.env.FAUSTUS_LARGE_MESSAGE_FIXTURE;
const text = exact ? JSON.parse(readFileSync(exact, 'utf8')).message : ('á漢🙂\r\n'.repeat(600000) + 'TAIL-UNIQUE');
assert.ok(text.length > 3_000_000);
const digest = value => createHash('sha256').update(value).digest('hex');
const originalHash = digest(text);
let checks = 0;
function check(name, fn) { fn(); checks++; console.log('ok:', name); }
check('all presentation sections reconstruct exact Unicode source', () => {
  for (const value of [text, 'x'.repeat(3900000), '🙂\r\n'.repeat(7000), ' @"a long path.ts" FAU-12 '.repeat(5000)]) {
    const parts = h.messageSections(value);
    assert.equal(parts.map(s => value.slice(s.start, s.end)).join(''), value);
    for (const s of parts) {
      assert.ok(s.end - s.start <= h.MESSAGE_SECTION_SIZE);
      const a = value.charCodeAt(s.end - 1), b = value.charCodeAt(s.end);
      assert.ok(!(a >= 0xd800 && a <= 0xdbff && b >= 0xdc00 && b <= 0xdfff));
      assert.ok(!(a === 13 && b === 10));
    }
  }
});
check('literal search spans section boundaries, wraps and handles overlaps', () => {
  const value = 'x'.repeat(8190) + '🙂NEEDLE' + 'y'.repeat(17000);
  const at = h.findInMessage(value, '🙂NEEDLE');
  assert.equal(at, 8190); assert.ok(h.messageContext(value, at).includes('🙂NEEDLE'));
  assert.equal(h.findInMessage(value, '🙂NEEDLE', at + 1), at);
  assert.equal(h.findInMessage(value, 'missing'), -1);
  assert.equal(h.findInMessage('aaaa', 'aaa', 1), 1);
});
check('editing first, middle and last sections preserves every other code unit', () => {
  for (const index of [0, Math.floor(h.messageSections(text).length / 2), h.messageSections(text).length - 1]) {
    const edit = h.startMessageEdit(text, index), s = edit.sections[index];
    assert.equal(h.completeMessageEdit(edit), text);
    edit.draft += 'á漢🙂\r\nEDIT';
    assert.equal(h.completeMessageEdit(edit), text.slice(0, s.end) + 'á漢🙂\r\nEDIT' + text.slice(s.end));
  }
});
check('textarea normalization preserves unchanged CRLF and CR around edits', () => {
  assert.equal(h.textareaChange('a\r\nb\rc\n🙂', 'a\nbX\nc\n🙂'), 'a\r\nbX\rc\n🙂');
  assert.equal(h.textareaChange('a\r\nb\r\n', 'a\nb\n'), 'a\r\nb\r\n');
  assert.equal(h.textareaOffset('a\r\nb', 2), 3);
});
check('huge paste retains all data while the next textarea remains bounded', () => {
  const edit = h.startMessageEdit(text), s = edit.sections[0];
  const pasted = '🙂\r\n'.repeat(500000);
  const next = h.updateMessageEdit(edit, pasted + edit.draft, pasted.length);
  assert.ok(next.edit.draft.length <= h.MESSAGE_SECTION_SIZE);
  assert.equal(h.completeMessageEdit(next.edit), pasted + text);
  assert.ok(next.caret >= 0 && next.caret <= next.edit.draft.length);
});

async function mount(module, value, props = {}) {
  const host = document.createElement('div'); document.body.append(host);
  const rendering = module.createRoot(host);
  const turn = { role: 'user', id: 'user', dbId: 'db', text: value, attachments: [], steps: [] };
  const calls = [];
  const render = (extra = {}) => rendering.render(module.React.createElement(module.UserTurn, {
    turn, busy: false, onEdit: (...args) => calls.push(args), onRegenerate: () => {}, onDelete: () => {}, ...props, ...extra,
  }));
  await module.React.act(() => render());
  return { host, calls, render: async extra => module.React.act(() => render(extra)),
    close: async () => { await module.React.act(() => rendering.unmount()); host.remove(); } };
}
async function click(module, element) { assert.ok(element); await module.React.act(async () => { element.click(); await Promise.resolve(); }); }
async function fill(module, element, value) {
  const type = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement : window.HTMLInputElement;
  await module.React.act(() => {
    Object.getOwnPropertyDescriptor(type.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}
const find = (host, label) => [...host.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label || b.textContent === label);

// Causal baseline uses the committed function body in RAM, not a reconstructed lookalike.
const baselineSource = execFileSync('git', ['show', '2b553700efec048fc8cd28ab725dd08add9c5a6b:' + transcript], { cwd: root, encoding: 'utf8', maxBuffer: 2 * 1024 * 1024 });
const baseline = await product(baselineSource, 'before');
const before = await mount(baseline, text);
assert.equal(before.host.querySelector('.fs-turn__bubble').textContent, text);
console.log('causal before:', text.length, 'mounted code units');
await before.close();

const view = await mount(p, text);
assert.ok(view.host.querySelector('.fs-large-message__text').textContent.length <= h.MESSAGE_SECTION_SIZE);
assert.ok(view.host.textContent.length < 10000);
await click(p, find(view.host, 'Next section'));
assert.equal(view.host.querySelector('.fs-large-message__text').textContent, text.slice(h.messageSections(text)[1].start, h.messageSections(text)[1].end));
await fill(p, view.host.querySelector('input[type=number]'), String(h.messageSections(text).length));
await click(p, find(view.host, 'Go to section'));
assert.ok(find(view.host, 'Next section').disabled);
console.log('ok: real UserTurn mounts one section and navigates to final section'); checks++;

const tail = text.trimEnd();
const query = tail.slice(tail.lastIndexOf('\n') + 1).slice(-40);
assert.ok(query);
await fill(p, view.host.querySelector('input[type=search]'), query);
view.host.querySelector('.fs-large-message__text').scrollTop = 10000;
await click(p, find(view.host, 'Find next'));
assert.ok(view.host.querySelector('.fs-large-message__text').textContent.includes(query));
assert.equal(view.host.querySelector('.fs-large-message__text').scrollTop, 0);
assert.ok(view.host.querySelector('[role=status]').textContent.includes('Match in section'));
console.log('ok: real full-source search exposes tail context'); checks++;

let copied;
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { copied = value; } } });
await click(p, find(view.host, 'Copy'));
assert.equal(digest(copied), originalHash);
console.log('ok: Clipboard API receives exact complete original'); checks++;
await view.close();

const fallback = await mount(p, text);
navigator.clipboard.writeText = async () => { throw new Error('CPU denied clipboard'); };
document.execCommand = action => {
  assert.equal(action, 'copy'); copied = document.querySelector('textarea[readonly]').value; return true;
};
await click(p, find(fallback.host, 'Copy'));
assert.equal(copied.replace(/\r\n?/g, '\n'), text.replace(/\r\n?/g, '\n'));
assert.equal(document.querySelector('textarea[readonly]'), null);
assert.ok(fallback.host.textContent.length < 10000);
console.log('ok: existing clipboard fallback is transient and removes its field'); checks++;
await fallback.close();

const editing = await mount(p, text);
await click(p, find(editing.host, 'Edit'));
const field = editing.host.querySelector('textarea');
assert.ok(field.value.length <= h.MESSAGE_SECTION_SIZE);
const first = h.messageSections(text)[0];
await fill(p, field, field.value + '🙂CHANGED');
await click(p, find(editing.host, 'Just save'));
assert.equal(editing.calls.length, 1);
assert.equal(editing.calls[0][1], text.slice(0, first.end) + '🙂CHANGED' + text.slice(first.end));
assert.equal(editing.calls[0][2], false);
console.log('ok: actual editor saves complete message with exact local change'); checks++;
await editing.close();

const unchanged = await mount(p, text);
await click(p, find(unchanged.host, 'Edit'));
await click(p, find(unchanged.host, 'Save and regenerate'));
assert.equal(digest(unchanged.calls[0][1]), originalHash); assert.equal(unchanged.calls[0][2], true);
await unchanged.close();
const reload = await mount(p, text);
assert.ok(reload.host.textContent.length < 10000); await reload.close();
console.log('ok: unchanged regenerate and fresh mount preserve source'); checks++;

const pasteView = await mount(p, text);
await click(p, find(pasteView.host, 'Edit'));
const pasteField = pasteView.host.querySelector('textarea');
pasteField.setSelectionRange(0, 0);
const inserted = 'á漢🙂\r\n'.repeat(350000);
await p.React.act(() => {
  const event = new window.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: { getData: () => inserted } });
  pasteField.dispatchEvent(event); assert.ok(event.defaultPrevented);
});
assert.ok(pasteView.host.querySelector('textarea').value.length <= h.MESSAGE_SECTION_SIZE);
await p.React.act(() => pasteView.host.querySelector('textarea').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true })));
assert.equal(pasteView.calls[0][1], inserted + text); assert.equal(pasteView.calls[0][2], true);
await pasteView.close();
console.log('ok: actual paste stays bounded and keyboard regenerate submits entire edit'); checks++;

const cancelView = await mount(p, text);
await click(p, find(cancelView.host, 'Edit'));
await p.React.act(() => cancelView.host.querySelector('textarea').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
assert.equal(cancelView.host.querySelector('textarea'), null); assert.equal(cancelView.calls.length, 0);
await cancelView.close();
console.log('ok: Escape cancels without changing the message'); checks++;

const growView = await mount(p, 'short🙂');
await click(p, find(growView.host, 'Edit'));
const growField = growView.host.querySelector('textarea');
growField.setSelectionRange(growField.value.length, growField.value.length);
await p.React.act(() => {
  const event = new window.Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: { getData: () => inserted } });
  growField.dispatchEvent(event); assert.ok(event.defaultPrevented);
});
assert.ok(growView.host.querySelector('textarea').value.length <= h.MESSAGE_SECTION_SIZE);
await click(p, find(growView.host, 'Just save'));
assert.equal(growView.calls[0][1], 'short🙂' + inserted);
await growView.close();
console.log('ok: a huge paste into a short edit switches to sections before mounting'); checks++;

const opened = [], issues = [];
const smallText = 'normal @src/file.py FAU-12';
const small = await mount(p, smallText, { onOpenFile: path => opened.push(path), boardKey: 'FAU', onOpenBoardIssue: id => issues.push(id) });
assert.equal(small.host.querySelector('[data-testid=large-message]'), null);
await click(p, small.host.querySelector('.fs-turn__mention'));
await click(p, small.host.querySelector('.fs-issue-chip'));
assert.deepEqual(opened, ['src/file.py']); assert.deepEqual(issues, ['FAU-12']);
await small.close();
console.log('ok: normal message, file mention and issue action unchanged'); checks++;

const longMentionText = 'x'.repeat(8170) + ' @"a long source file.ts" FAU-12 ' + 'y'.repeat(17000);
const longMention = await mount(p, longMentionText, { onOpenFile: path => opened.push(path), boardKey: 'FAU', onOpenBoardIssue: id => issues.push(id) });
await click(p, find(longMention.host, 'Next section'));
await click(p, longMention.host.querySelector('.fs-turn__mention'));
assert.equal(opened.at(-1), 'a long source file.ts');
await longMention.close();
console.log('ok: a quoted mention crossing the original section boundary stays actionable'); checks++;

p.setLang('es', { persist: false });
const spanish = await mount(p, text);
assert.ok(find(spanish.host, 'Sección siguiente'));
assert.equal(spanish.host.querySelector('input[type=search]').getAttribute('aria-label'), 'Buscar en todo el mensaje');
await spanish.close();
assert.equal(requests, 0);
assert.equal(digest(text), originalHash);
console.log(JSON.stringify({ status: 'PASS_CPU_DOM_ONLY', checks: checks + 1, fixture_chars: text.length,
  fixture_sha256: originalHash, mounted_section_max: h.MESSAGE_SECTION_SIZE, API_calls: requests,
  layout_AX_browser_tested: false }));
stop();
assert.equal(document.body.childElementCount, 0, 'all simulated roots and fallback fields are removed');
await window.happyDOM.close();
