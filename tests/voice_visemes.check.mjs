// Agora #251: the pure part of the orb's mouth and the karaoke subtitle (studio/src/voice/visemes.ts).
// Driven by tests/test_orb_visemes_251_js.py, or by hand:  node tests/voice_visemes.check.mjs
// With MARKS_JSON and MARKS_TEXT set it also reads the marks the real server produced for that text.
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-visemes-')), 'visemes.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'voice', 'visemes.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const V = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const text = 'Hola, mundo. ¿Qué tal?';
const raw = () => ({
  version: 1, language: 'es', duration_ms: 2000, timing: 'audio', words_source: 'estimated', visemes_source: 'estimated',
  words: [
    { i: 0, text: 'Hola', start_char: 0, end_char: 4, start_ms: 100, end_ms: 500 },
    { i: 1, text: 'mundo', start_char: 6, end_char: 11, start_ms: 600, end_ms: 1000 },
    { i: 2, text: 'Qué', start_char: 14, end_char: 17, start_ms: 1300, end_ms: 1500 },
    { i: 3, text: 'tal', start_char: 18, end_char: 21, start_ms: 1500, end_ms: 1800 },
  ],
  visemes: [
    { v: 'rest', start_ms: 0, end_ms: 100 }, { v: 'O', start_ms: 100, end_ms: 250 }, { v: 'L', start_ms: 250, end_ms: 350 },
    { v: 'AA', start_ms: 350, end_ms: 500 }, { v: 'MBP', start_ms: 600, end_ms: 700 }, { v: 'U', start_ms: 700, end_ms: 800 },
    { v: 'OTHER', start_ms: 800, end_ms: 900 }, { v: 'FV', start_ms: 900, end_ms: 1000 }, { v: 'E', start_ms: 1300, end_ms: 1500 },
    { v: 'I', start_ms: 1500, end_ms: 1800 },
  ],
});

// ── Reading the marks ──
const marks = V.parseMarks(raw(), text);
assert(marks && marks.words.length === 4 && marks.visemes.length === 10, 'valid marks are read');
eq([marks.words[1].startChar, marks.words[1].endMs, marks.wordsSource, marks.timing], [6, 1000, 'estimated', 'audio'], 'fields are renamed to camelCase');
const bad = (mutate, msg, t = text) => { const r = raw(); mutate(r); assert(V.parseMarks(r, t) === null, msg); };
bad(r => { r.version = 2; }, 'unknown version is rejected');
bad(r => { r.words[1].start_char = 7; r.words[1].end_char = 12; }, 'a word that is not at its offset in the text is rejected (nothing lit beats the wrong word lit)');
bad(() => {}, 'marks for another text are rejected', 'Adiós, mundo. ¿Qué tal?');
bad(r => { r.words[1].start_ms = 400; }, 'overlapping words are rejected');
bad(r => { r.words[0].end_ms = 50; }, 'a word that ends before it starts is rejected');
bad(r => { r.visemes[1].v = 'XX'; }, 'an unknown viseme is rejected');
bad(r => { r.visemes[2].start_ms = 200; }, 'overlapping visemes are rejected');
bad(r => { delete r.words; }, 'missing words are rejected');
assert(V.parseMarks(null, text) === null && V.parseMarks('x', text) === null && V.parseMarks({}, text) === null, 'null, a string and {} are not marks');

// ── What is being said at a given moment ──
eq([V.wordAt(marks, 0), V.wordAt(marks, 99), V.wordAt(marks, 100), V.wordAt(marks, 499)], [-1, -1, 0, 0], 'word: none before the first, lit from its start');
eq([V.wordAt(marks, 500), V.wordAt(marks, 599)], [0, 0], 'word: kept lit through the short pause after it');
eq([V.wordAt(marks, 600), V.wordAt(marks, 999)], [1, 1], 'word: the next one takes over at its start');
eq([V.wordAt(marks, 1299), V.wordAt(marks, 1300)], [1, 2], 'word: held until the next one begins');
const slow = raw(); slow.words[2].start_ms = 1600; slow.words[2].end_ms = 1700; slow.words[3].start_ms = 1700; slow.words[3].end_ms = 1800;
const slowMarks = V.parseMarks(slow, text);
eq([V.wordAt(slowMarks, 1000 + V.WORD_HOLD_MS - 1), V.wordAt(slowMarks, 1000 + V.WORD_HOLD_MS), V.wordAt(slowMarks, 1500)], [1, -1, -1], 'word: a long pause dims the subtitle');
eq([V.wordAt(marks, 1500), V.wordAt(marks, 1799), V.wordAt(marks, 1800), V.wordAt(marks, 1800 + V.WORD_HOLD_MS)], [3, 3, 3, -1], 'word: the last one holds, then none');
let previous = -1, visited = new Set(), monotonic = true;
for (let ms = 0; ms <= 2200; ms++) {
  const k = V.wordAt(marks, ms);
  if (k >= 0) { if (k < previous) monotonic = false; previous = k; visited.add(k); }
}
assert(monotonic && visited.size === 4, 'scanning the audio ms by ms lights every word, in order, never going back');
eq([V.visemeAt(marks, 120), V.visemeAt(marks, 349), V.visemeAt(marks, 350), V.visemeAt(marks, 500), V.visemeAt(marks, 550)], ['O', 'L', 'AA', 'rest', 'rest'], 'viseme: [start, end) and rest in the gaps');
eq([V.visemeAt(null, 100), V.visemeAt(marks, 99999), V.visemeAt(marks, -5)], ['rest', 'rest', 'rest'], 'viseme: no marks or out of range is a closed mouth');

// ── Easing the mouth ──
const rest = V.MOUTHS.rest, aa = V.MOUTHS.AA;
let m = rest;
for (let i = 0; i < 6; i++) m = V.stepMouth(m, aa, 33);
assert(m.open > 0.6 && m.open < 1, 'after ~200 ms the mouth is mostly open but never overshoots');
const once = V.stepMouth(rest, aa, 100), twice = V.stepMouth(V.stepMouth(rest, aa, 50), aa, 50);
assert(Math.abs(once.open - twice.open) < 1e-9, 'easing does not depend on the frame rate');
eq(V.stepMouth(rest, aa, 0), rest, 'no time, no movement');
const short = V.stepMouth(V.stepMouth(rest, aa, 40), V.MOUTHS.MBP, 40);
assert(short.open > 0 && short.press > 0, 'a 40 ms viseme still shows before the next one takes over');

// ── The orb's shape ──
const shapes = V.VISEMES.map(v => ({ v, s: V.orbShape(V.MOUTHS[v], false) }));
const key = s => [s.sx, s.sy, s.ringX, s.ringY].map(x => x.toFixed(3)).join('|');
assert(new Set(shapes.map(x => key(x.s))).size === V.VISEMES.length, 'every viseme draws a different orb');
const by = Object.fromEntries(shapes.map(x => [x.v, x.s]));
assert(by.AA.ringY > by.E.ringY && by.E.ringY > by.I.ringY && by.I.ringY > by.MBP.ringY, 'the mouth ring opens: a > e > i > m');
assert(by.I.ringX > by.U.ringX && by.E.sx > by.O.sx, 'wide for i and e, narrow for u and o');
assert(by.MBP.sy < 1 && by.AA.sy > 1, 'lips pressed shrink the orb, an open vowel stretches it');
eq([by.rest.sx, by.rest.sy, by.rest.ringX, by.rest.ringY], [1, 1, 1.28, 0.3], 'a closed mouth is the orb as it always was');
assert(V.VISEMES.every(v => key(V.orbShape(V.MOUTHS[v], true)) === key(V.orbShape(V.MOUTHS.rest, true))), 'with reduced motion no viseme deforms the orb');

// ── The subtitle ──
const parts = V.subtitleParts(text, marks);
eq(parts.map(p => p.text).join(''), text, 'the subtitle parts joined are exactly the text');
eq(parts.filter(p => p.word !== null).map(p => [p.word, p.text]), [[0, 'Hola'], [1, 'mundo'], [2, 'Qué'], [3, 'tal']], 'the words are cut out by index');
eq(V.subtitleParts('Hola', null), [{ text: 'Hola', word: null }], 'without marks the subtitle is one plain part');
eq(V.subtitleParts('', null), [], 'no text, no parts');
eq([V.heardMs(1, 0.05), V.heardMs(1, 5), V.heardMs(0.01, 0.1), V.heardMs(2, undefined)], [950, 700, 0, 2000], 'heard position: output latency subtracted, capped at 300 ms, never negative');

// ── Marks made by the real server ──
if (process.env.MARKS_JSON) {
  const real = V.parseMarks(JSON.parse(readFileSync(process.env.MARKS_JSON, 'utf8')), readFileSync(process.env.MARKS_TEXT, 'utf8'));
  assert(real && real.words.length > 3 && real.visemes.length > 10, 'marks produced by the Python service are accepted unchanged');
  const used = new Set(real.visemes.map(v => v.v));
  assert(['AA', 'E', 'O', 'MBP'].every(v => used.has(v)), 'and use the classes the sentence needs');
}

console.log(failed ? `${failed} FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
