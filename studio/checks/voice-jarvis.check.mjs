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

// --- stop phrases ------------------------------------------------------------
for (const phrase of STOP_PHRASES) assert.equal(isStopPhrase(phrase), true, `expected stop phrase: ${phrase}`);
assert.equal(isStopPhrase('STOP!'), true);
assert.equal(isStopPhrase('¡Para!'), true);
assert.equal(isStopPhrase('Espera'), true);
assert.equal(isStopPhrase('stop talking about that'), false); // whole-utterance only
assert.equal(isStopPhrase('can you stop'), false);
assert.equal(isStopPhrase(''), false);

// --- hallucination list ------------------------------------------------------------
for (const phrase of HALLUCINATIONS) assert.equal(isHallucination(phrase), true, `expected hallucination: ${phrase}`);
assert.equal(isHallucination('Thank you.'), true);
assert.equal(isHallucination('Subtítulos realizados por la comunidad de Amara.org'), true);
assert.equal(isHallucination('ok'), true); // <= 2 chars after normalizing
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

// --- normalize ------------------------------------------------------------
assert.equal(normalizeUtterance('¡CÁLLATE, por favor!'), 'callate por favor');

// --- TurnDetector at 900ms ------------------------------------------------------------
const detector = new TurnDetector(900);
for (let now = 50; now < 1000; now += 50) assert.equal(detector.push(0, now), 'continue');
for (let now = 1000; now <= 1400; now += 50) detector.push(0.1, now);
assert.equal(detector.push(0, 2200), 'continue'); // 800ms of silence: turn stays open
assert.equal(detector.push(0, 2350), 'silence'); // 950ms of silence: turn ends
assert.equal(new TurnDetector().silenceMs, 900); // new default (was 1200)

// --- barge-in constants ------------------------------------------------------------
assert.ok(BARGE_IN_THRESHOLD > 0.025); // stricter than the base VAD threshold
assert.equal(BARGE_IN_MS, 250);

console.log('ALL OK: similarity, echo guard, stop phrases, hallucinations, wake word, 900ms turn detection');
