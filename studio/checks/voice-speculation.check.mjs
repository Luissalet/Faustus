// Speculative transcription (radar #249): a short pause sends the recording so
// far to speech-to-text while the end of the turn is still being confirmed;
// speaking again withdraws the guess (and aborts its request); a turn that
// ends with no voice after the snapshot uses that transcript; at most a few
// guesses per turn; off when eagerMs is 0 or nothing was really said.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
const result = await build({ entryPoints: ['studio/src/voice/engine.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { TurnDetector, SpeculativeTranscript } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

function speak(d, from, to) { for (let t = from; t <= to; t += 50) d.push(0.1, t); }
function quiet(d, from, to) { for (let t = from; t <= to; t += 50) d.push(0.0, t); }

// 1. pause → snapshot → the turn ends quietly → the guess is the answer.
{
  const d = new TurnDetector(900); const s = new SpeculativeTranscript(350);
  speak(d, 0, 600); quiet(d, 650, 900);
  assert.equal(s.shouldSnapshot(d, 900), false, 'not before 350 ms of quiet');
  quiet(d, 950, 1000);
  assert.equal(s.shouldSnapshot(d, 1000), true, 'after 350 ms of quiet');
  assert.equal(s.shouldSnapshot(d, 1600), false, 'not once the real end of the turn has come');
  s.want(1000);
  let aborted = false;
  s.start(Promise.resolve('hola qué tal'), () => { aborted = true; });
  assert.equal(s.shouldSnapshot(d, 1100), false, 'one guess at a time');
  quiet(d, 1050, 1600);
  const taken = s.take();
  assert.ok(taken, 'the guess covers the turn');
  assert.equal(await taken, 'hola qué tal');
  assert.equal(aborted, false);
}

// 2. pause → snapshot → the person speaks again → withdrawn and aborted.
{
  const d = new TurnDetector(900); const s = new SpeculativeTranscript(350);
  speak(d, 0, 600); quiet(d, 650, 1000);
  s.want(1000);
  let aborted = false;
  s.start(Promise.resolve('hola'), () => { aborted = true; });
  s.voice(1200);
  assert.equal(aborted, true, 'the request is aborted');
  assert.equal(s.withdrawn, 1);
  assert.equal(s.take(), null, 'nothing speculative left');
}

// 3. voice between the request for data and the chunk: never started.
{
  const s = new SpeculativeTranscript(350);
  s.want(1000); s.voice(1100);
  let aborted = false;
  s.start(Promise.resolve('x'), () => { aborted = true; });
  assert.equal(aborted, true);
  assert.equal(s.take(), null);
}

// 4. at most maxPerTurn guesses, off at 0, and not before real speech.
{
  const d = new TurnDetector(900); const s = new SpeculativeTranscript(350, 2);
  speak(d, 0, 300); quiet(d, 350, 700);
  for (let i = 0; i < 3; i++) {
    if (s.shouldSnapshot(d, 700 + i)) { s.want(700 + i); s.start(Promise.resolve('a'), () => {}); s.voice(800 + i); }
  }
  assert.equal(s.started, 2);
  const off = new SpeculativeTranscript(0);
  assert.equal(off.shouldSnapshot(d, 700), false);
  const fresh = new TurnDetector(900);
  quiet(fresh, 0, 600);
  assert.equal(new SpeculativeTranscript(350).shouldSnapshot(fresh, 600), false, 'no speech, no guess');
}

// 5. drop() on cancel aborts what is in flight.
{
  const s = new SpeculativeTranscript(350);
  s.want(10); let aborted = false; s.start(Promise.resolve('x'), () => { aborted = true; });
  s.drop();
  assert.equal(aborted, true); assert.equal(s.take(), null);
}
console.log('ALL OK: speculative transcript held on a pause, withdrawn on voice, used when the turn ends quietly, bounded');
