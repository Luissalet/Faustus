import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const window = new Window({ url: 'http://localhost/projects/p?tab=tasks' });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'MutationObserver', 'Event', 'CustomEvent', 'getComputedStyle']) Object.defineProperty(globalThis, key, { configurable: true, value: key === 'getComputedStyle' ? window.getComputedStyle.bind(window) : key === 'window' ? window : window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
for (const key of ['NodeFilter', 'Node', 'Element']) globalThis[key] = window[key];
globalThis.requestAnimationFrame = callback => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = clearTimeout;
const bundled = await build({ stdin: { contents: `export {ProjectTasks} from './studio/src/screens/project/Tasks';export {createElement, act} from 'react';export {createRoot} from 'react-dom/client';`, resolveDir: process.cwd(), loader: 'ts' }, bundle: true, format: 'esm', platform: 'node', write: false, loader: { '.css': 'empty' } });
const output = join(mkdtempSync(join(tmpdir(), 'project-tasks-check-')), 'view.mjs');
writeFileSync(output, bundled.outputFiles[0].text);
const m = await import(pathToFileURL(output));
const host = document.createElement('div'); document.body.append(host);
const root = m.createRoot(host);
const original = {id: 42, title: 'Original objective', body: 'Full description '.repeat(100), acceptance: 'Verify result', priority: 2, owner: 'builder', status: 'claimed', kind:'feature', labels:[], etag: 'etag-read', thread_id: 7};
let task = {...original};
let conflict = true;
let edits = [];
globalThis.fetch = async (path, init = {}) => {
  if (init.method === 'PATCH') {
    edits.push(JSON.parse(init.body));
    if (conflict) return Response.json({detail:'The task changed. Reload it before saving.'}, {status:409});
    task = {...task, ...edits.at(-1), etag:'etag-saved'};
    return Response.json({ok:true,task});
  }
  if (path.endsWith('/prepare')) return Response.json({detail:'Old record is too large'}, {status:400});
  if (path.endsWith('/42')) return Response.json({ok:true,task,messages:[{id:1,author:'builder',body:'Prior clarification',created:1800000000}],edits:[{revision:1,author:'editor',created:1800000000,before:{acceptance:'Original criterion'},after:{acceptance:'Revised criterion'}}]});
  return Response.json({ok:true,space_id:'silver',tasks:[task]});
};
const tick = () => new Promise(resolve => setTimeout(resolve, 20));
await m.act(async () => { root.render(m.createElement(m.ProjectTasks, {project:{id:'p'},say:()=>{}})); await tick(); });
assert.equal(host.querySelectorAll('[data-testid="project-task-card"]').length,1,'agent task is visible immediately');
assert.equal(host.querySelectorAll('details').length,0,'no parallel hidden objectives/requirements/board');
assert.match(host.textContent,/Old record is too large/,'migration errors are visible without hiding canonical tasks');
await m.act(async () => { host.querySelector('[data-testid="project-task-card"]').click(); await tick(); });
let dialog = document.querySelector('[data-testid="project-task-editor"]');
assert.ok(dialog && !host.querySelector('.fs-project-task-column').contains(dialog),'editor is a modal outside the narrow column');
assert.equal(dialog.querySelector('textarea').value,original.body,'full description comes from task detail');
assert.match(dialog.textContent,/Prior clarification/);
assert.match(dialog.textContent,/Original criterion/);
assert.match(dialog.textContent,/Revised criterion/);
const type = (element, value) => {
  const prototype = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype,'value').set.call(element,value);
  element.dispatchEvent(new window.Event('input',{bubbles:true}));
};
await m.act(async () => type(dialog.querySelector('textarea'), 'Luis clarified the goal'));
await m.act(async () => {
  for (const [name, value] of [['Status','done'], ['Type','idea']]) {
    const select = [...dialog.querySelectorAll('label')].find(el => el.textContent.startsWith(name)).querySelector('select');
    Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set.call(select,value);
    select.dispatchEvent(new window.Event('change',{bubbles:true}));
  }
  const labels = [...dialog.querySelectorAll('label')].find(el => el.textContent.startsWith('Labels')).querySelector('input');
  type(labels, 'review, clarified');
});
await m.act(async () => { dialog.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})); await tick(); });
assert.equal(edits[0].expected_etag,'etag-read');
assert.equal(edits[0].body,'Luis clarified the goal');
assert.equal(edits[0].status,'done');
assert.equal(edits[0].kind,'idea');
assert.deepEqual(edits[0].labels,['review','clarified']);
assert.ok(document.querySelector('[role="alert"]'));
assert.equal(dialog.querySelector('textarea').value,'Luis clarified the goal','conflict retains the unsaved draft');
conflict = false;
await m.act(async () => { dialog.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})); await tick(); });
assert.equal(task.body,'Luis clarified the goal');
assert.equal(document.querySelector('[data-testid="project-task-editor"]'),null,'success closes the modal');
await m.act(async () => root.unmount());
await window.happyDOM.abort();
console.log('PASS: canonical agent tasks visible, modal full detail/history, CAS edits, conflict draft retained, successful save.');
process.exit(0);
