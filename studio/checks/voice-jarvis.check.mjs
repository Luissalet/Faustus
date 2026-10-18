import assert from 'node:assert/strict';
import { build } from 'esbuild';
const result = await build({ entryPoints: ['studio/src/voice/engine.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const {
  TurnDetector, similarity, isEcho, isStopPhrase, isHallucination, stripWakeWord, normalizeUtterance,
  STOP_PHRASES, HALLUCINATIONS, BARGE_IN_THRESHOLD, BARGE_IN_MS,
} = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

// --- similarity helper ---------------------------------------------------
assert.equal(similarity('Hello there', 'Hello there'), 1);
assert.equal(similarity('', ''), 1);
assert.equal(similarity('Hello', ''), 0);
assert.ok(similarity('You can order pizza online', 'you can order pizza, online!') >= 0.99);
assert.ok(similarity('I like cats', 'The weather is nice today') < 0.3);
assert.ok(similarity('Turn on the lights', 'Turn off the lights') > 0.5);

// --- echo guard ------------------------------------------------------------
// Heard right after Faustus finished a near-identical sentence: discard.
assert.equal(isEcho('you can order pizza online', 'You can order pizza online.', 1500, 400), true);
// Same words, but too long after playback ended: not an echo.
assert.equal(isEcho('you can order pizza online', 'You can order pizza online.', 1500, 4000), false);
// Genuinely different utterance shortly after playback: not an echo.
assert.equal(isEcho('what time is it', 'You can order pizza online.', 1500, 400), false);
// A correction can share almost every word with playback and still change the action.
for (const [heard, spoken] of [
  ['Turn off the lights in the living room.', 'Turn on the lights in the living room.'],
  ['Apaga las luces de la sala de estar.', 'Enciende las luces de la sala de estar.'],
  ['Do not delete the files in this folder.', 'Do delete the files in this folder.'],
  ['No borres los archivos de esta carpeta.', 'Borra los archivos de esta carpeta.'],
  ['Set the timer for twenty minutes please.', 'Set the timer for ten minutes please.'],
  ['Pon el temporizador en veinte minutos por favor.', 'Pon el temporizador en diez minutos por favor.'],
  ['Move Alice before Bob in the list.', 'Move Bob before Alice in the list.'],
  ['Mueve a Ana antes de Luis en la lista.', 'Mueve a Luis antes de Ana en la lista.'],
  ['Yes.', 'Yes.'], ['Sí.', 'Sí.'], ['No.', 'No.'], ['Gracias.', 'Gracias.'],
]) assert.equal(isEcho(heard, spoken, 1500, 400), false, `keep real reply: ${heard}`);
// A complete contiguous tail of playback remains echo, including punctuation variants.
assert.equal(isEcho('puedes abrir el informe', 'Ya está listo; puedes abrir el informe.', 1500, 400), true);
assert.equal(isEcho('you can open the report', 'All done. You can open the report.', 1500, 400), true);
assert.equal(isEcho('', 'All done.', 1500, 400), false);

// --- stop phrases ------------------------------------------------------------
for (const phrase of STOP_PHRASES) assert.equal(isStopPhrase(phrase), true, `expected stop phrase: ${phrase}`);
assert.equal(isStopPhrase('STOP!'), true);
assert.equal(isStopPhrase('¡Para!'), true);
assert.equal(isStopPhrase('Espera'), true);
assert.equal(isStopPhrase('stop talking about that'), false); // whole-utterance only
assert.equal(isStopPhrase('can you stop'), false);
assert.equal(isStopPhrase(''), false);
// extra (server-configured) stop phrases merge with, never replace, the built-ins
assert.equal(isStopPhrase('enough', ['enough', 'basta']), true);
assert.equal(isStopPhrase('basta ya'), false); // whole-utterance only, still applies to extras
assert.equal(isStopPhrase('stop', ['enough']), true); // built-ins still work when extras are passed
assert.equal(isStopPhrase('nonsense', []), false);
assert.equal(isStopPhrase('nonsense', undefined), false);

// --- hallucination list ------------------------------------------------------------
for (const phrase of HALLUCINATIONS) assert.equal(isHallucination(phrase), true, `expected hallucination: ${phrase}`);
assert.equal(isHallucination('Thank you.'), false);
assert.equal(isHallucination('Subtítulos realizados por la comunidad de Amara.org'), true);
for (const phrase of ['ok', 'sí', 'no', '42', 'gracias', 'you', 'subtítulos en español']) {
  assert.equal(isHallucination(phrase), false, `must preserve speech: ${phrase}`);
}
assert.equal(isHallucination('you are right about that'), false);
assert.equal(isHallucination('what is the weather today'), false);

// --- wake word ------------------------------------------------------------
assert.deepEqual(stripWakeWord('Faustus, what time is it'), { matched: true, text: 'what time is it' });
assert.deepEqual(stripWakeWord('faustus what time is it'), { matched: true, text: 'what time is it' });
assert.deepEqual(stripWakeWord('Hey Faustus what time is it'), { matched: true, text: 'Hey what time is it' });
assert.deepEqual(stripWakeWord('Faustos dime la hora'), { matched: true, text: 'dime la hora' });
assert.deepEqual(stripWakeWord('Fausto dime la hora'), { matched: true, text: 'dime la hora' });
assert.deepEqual(stripWakeWord('Faust us what time is it'), { matched: true, text: 'what time is it' });
assert.deepEqual(stripWakeWord('what time is it Faustus'), { matched: false, text: 'what time is it Faustus' });
assert.deepEqual(stripWakeWord('what time is it'), { matched: false, text: 'what time is it' });
for (const text of ['Fosters, abre el informe.', 'Foustas: abre el informe.', 'Hola Foustas, abre el informe.']) {
  assert.equal(stripWakeWord(text).matched, true, text);
  assert.ok(!/fosters|foustas/i.test(stripWakeWord(text).text), text);
}
for (const text of ['Foster sent the report.', 'Foster ha enviado el informe.', 'Fosters makes beer.',
  'I read Fosters, yesterday.', 'Fosters abre el informe.', "Foster's, open the report."]) {
  assert.deepEqual(stripWakeWord(text), { matched: false, text }, text);
}

// --- normalize ------------------------------------------------------------
assert.equal(normalizeUtterance('¡CÁLLATE, por favor!'), 'callate por favor');

// --- TurnDetector at 900ms ------------------------------------------------------------
const detector = new TurnDetector(900);
for (let now = 50; now < 1000; now += 50) assert.equal(detector.push(0, now), 'continue');
for (let now = 1000; now <= 1400; now += 50) detector.push(0.1, now);
assert.equal(detector.push(0, 2200), 'continue'); // 800ms of silence: turn stays open
assert.equal(detector.push(0, 2350), 'silence'); // 950ms of silence: turn ends
assert.equal(new TurnDetector().silenceMs, 900); // new default (was 1200)
const brief = new TurnDetector(900);
for (const now of [50, 100, 150]) brief.push(0.1, now);
assert.equal(brief.push(0, 1100), 'silence');
const click = new TurnDetector(900);
click.push(0.1, 50);
assert.equal(click.push(0, 15100), 'empty');

// --- barge-in constants ------------------------------------------------------------
assert.ok(BARGE_IN_THRESHOLD > 0.025); // stricter than the base VAD threshold
assert.equal(BARGE_IN_MS, 250);

console.log('ALL OK: similarity, echo guard, stop phrases, hallucinations, wake word, 900ms turn detection');
