// Behaviour of the "hand this note to the agent" prompt builder (FAUSTUS).
// Run by tests/test_note_to_agent_js.py under pytest. No DOM needed — the
// module guards its browser globals precisely so this can import it.

import test from 'node:test';
import assert from 'node:assert/strict';

const { buildPrompt } = await import('../static/js/noteToAgent.js');

test('a checklist hands over only the open items, with their real indexes', () => {
  const prompt = buildPrompt({
    id: 'abc123', title: 'Shopping',
    items: [
      { text: 'done thing', done: true },
      { text: 'buy milk', done: false },
      { text: 'call the plumber' },
    ],
  });
  assert.ok(!prompt.includes('done thing'), 'finished items must not be re-assigned');
  assert.match(prompt, /\[index 1\] buy milk/);
  assert.match(prompt, /\[index 2\] call the plumber/);
});

test('it tells the agent to tick items off in the real note', () => {
  const prompt = buildPrompt({ id: 'abc123', title: 'x', items: [{ text: 'a' }] });
  assert.match(prompt, /manage_notes/);
  assert.match(prompt, /action="toggle_item"/);
  assert.match(prompt, /id="abc123"/);
});

test('a freeform note hands over its body instead of a list', () => {
  const prompt = buildPrompt({ id: 'n1', title: 'Plan', content: 'Rewrite the README' });
  assert.match(prompt, /Rewrite the README/);
  assert.ok(!prompt.includes('Open items'), 'no checklist, no item list');
});

test('a fully ticked checklist does not pretend there is work left', () => {
  const prompt = buildPrompt({
    id: 'n2', title: 'Done', content: '', items: [{ text: 'a', done: true }],
  });
  assert.ok(!prompt.includes('Open items'));
});

test('it accepts the tool-side field name too', () => {
  const prompt = buildPrompt({ id: 'n3', title: 't', checklist_items: [{ text: 'ship it' }] });
  assert.match(prompt, /ship it/);
});

test('it asks for real work, not a description of the work', () => {
  const prompt = buildPrompt({ id: 'n4', title: 't', items: [{ text: 'a' }] });
  assert.match(prompt, /do not just describe/i);
});

test('an empty note still produces a usable prompt', () => {
  const prompt = buildPrompt({ id: 'n5' });
  assert.match(prompt, /untitled note/);
  assert.match(prompt, /\(the note is empty\)/);
});

test('a missing note object does not throw', () => {
  assert.doesNotThrow(() => buildPrompt(null));
});
