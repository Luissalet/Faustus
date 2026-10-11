/** Changing host language updates shared controls without losing the draft. */
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
const dom = new Window({url:'https://locale-host.example/'});
for (const key of ['window','document','HTMLElement','customElements','CustomEvent','Event','Node','ShadowRoot','MutationObserver','getComputedStyle']) {
  (globalThis as Record<string, unknown>)[key] = (dom as unknown as Record<string, unknown>)[key];
}
const { defineFaustusChat, FaustusChatElement } = await import('../src/faustus-chat.js');
defineFaustusChat();
const panel = document.createElement('faustus-chat') as InstanceType<typeof FaustusChatElement>;
panel.draft = 'Draft that must survive a language change';
panel.locale = 'es';
assert.equal(panel.shadowRoot!.querySelector('select')!.getAttribute('aria-label'),'Conversaciones del proyecto');
assert.equal(panel.shadowRoot!.querySelector('[data-label-en="Refresh"]')!.textContent,'Actualizar');
panel.locale = 'en';
assert.equal(panel.shadowRoot!.querySelector('select')!.getAttribute('aria-label'),'Project conversations');
for (const label of ['Refresh','Retry','Delete chat','Faustus settings']) {
  assert.equal(panel.shadowRoot!.querySelector(`[data-label-en="${label}"]`)!.textContent,label);
}
assert.equal(panel.shadowRoot!.querySelector('textarea')!.getAttribute('aria-label'),'Message');
assert.equal(panel.draft,'Draft that must survive a language change');
panel.locale = 'es';
assert.equal(panel.shadowRoot!.querySelector('[data-label-en="Faustus settings"]')!.textContent,'Ajustes de Faustus');
assert.equal(panel.draft,'Draft that must survive a language change');
panel.locale = 'en';
panel.session = 'existing';
panel.fetch = async () => Response.json({messages:[{role:'assistant',content:'Completed tool',metadata:{writer_tool_call:{auditIndex:1}}}]});
let undoCalls = 0;
panel.onToolUndo = async () => { undoCalls++; };
await panel.loadHistory();
const undo = panel.shadowRoot!.querySelector<HTMLButtonElement>('[data-label-en="Undo"]')!;
assert.ok(undo, 'Persisted Writer tool receipt provides the normal undo control');
undo.click();
await Promise.resolve();
assert.equal(undo.textContent,'Undone');
assert.equal(undo.disabled,true);
panel.locale = 'es';
assert.equal(undo.textContent,'Deshecho');
panel.locale = 'en';
assert.equal(undo.textContent,'Undone');
assert.equal(undo.disabled,true);
assert.equal(undoCalls,1);
assert.equal(panel.sessionId,'existing');
assert.equal(panel.draft,'Draft that must survive a language change');
console.log('PASS: shared host controls and completed undo switch language and preserve draft');
await dom.happyDOM.close();
