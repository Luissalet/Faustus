import assert from 'node:assert/strict';
import { build } from 'esbuild';

globalThis.window = { setTimeout, localStorage: { getItem: () => 'es' } };
const built = await build({ entryPoints: ['studio/src/screens/studio/fold-api.ts'], bundle: true, platform: 'node', format: 'esm', write: false, logLevel: 'silent' });
const fold = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));

// Which texts get the control, and which keys change the level.
const paragraph = 'Una frase de relleno suficientemente larga para ocupar sitio. '.repeat(8);
assert.equal(fold.isFoldable('corto'), false);
assert.equal(fold.isFoldable(paragraph.repeat(4)), false, 'one long paragraph has nothing to fold');
assert.equal(fold.isFoldable([paragraph, paragraph, paragraph].join('\n\n')), true);
assert.deepEqual(['1', '2', '3', '4', '5', '0', '6', 'a', 'Enter'].map(fold.foldLevelFromKey), [1, 2, 3, 4, 5, null, null, null, null]);
assert.equal(fold.textKey('abc'), fold.textKey('abc'));
assert.notEqual(fold.textKey('abc'), fold.textKey('abd'));
assert.equal(fold.foldLevelLabel(2), 'Frases clave');
assert.equal(fold.foldLevelLabel(5), 'Temas');

// First call sends the text; later levels of the same text ask by id; a dropped copy is re-sent.
const calls = [];
let responder;
globalThis.fetch = async (url, init) => {
  calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body) : null });
  const { status, body } = responder(calls.at(-1));
  return { ok: status < 400, status, json: async () => body };
};
const view = (level, text = 'x') => ({ id: 'fold_1', level, name: 'n', chars: text.length, original_chars: 99, items: [{ paragraph: 'p1', text }], text });
responder = (call) => call.method === 'POST' ? { status: 200, body: { document: { id: 'fold_1' }, view: view(2) } } : { status: 200, body: view(Number(new URL(call.url, 'http://x').searchParams.get('level'))) };
const text = [paragraph, paragraph, paragraph].join('\n\n');
assert.equal((await fold.foldView(text, 2)).level, 2);
assert.deepEqual(calls.at(-1), { url: '/api/fold', method: 'POST', body: { text, level: 2 } });
assert.equal((await fold.foldView(text, 3)).level, 3);
assert.equal(calls.at(-1).url, '/api/fold/fold_1?level=3');
assert.equal(calls.at(-1).method, 'GET');

responder = (call) => call.method === 'GET'
  ? { status: 404, body: { error: 'unknown fold id', error_class: 'fold.not_found' } }
  : { status: 200, body: { document: { id: 'fold_2' }, view: view(4) } };
assert.equal((await fold.foldView(text, 4)).level, 4);
assert.deepEqual(calls.slice(-2).map((c) => c.method), ['GET', 'POST']);

// A model that does not answer is reported as such, not as a generic failure.
responder = () => ({ status: 503, body: { error: 'no model', error_class: 'fold.model_unavailable' } });
await assert.rejects(fold.foldView(text, 5), (error) => error instanceof fold.FoldError && error.modelUnavailable && error.status === 503);
responder = () => ({ status: 400, body: { error: 'bad', error_class: 'fold.invalid' } });
await assert.rejects(fold.foldView(text + 'x', 3), (error) => error instanceof fold.FoldError && !error.modelUnavailable);

// Opening the original of a fragment goes through the same document.
responder = (call) => call.method === 'POST' ? { status: 200, body: { document: { id: 'fold_3' }, view: view(2) } } : { status: 200, body: { ...view(1, 'texto original'), text: 'texto original' } };
assert.equal(await fold.foldOriginal(text + 'y', { paragraph: 'p2' }), 'texto original');
assert.equal(calls.at(-1).url, '/api/fold/fold_3?level=1&paragraph=p2');
console.log('text-fold.check ok');
