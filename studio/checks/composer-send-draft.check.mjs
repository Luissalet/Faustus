/**
 * The composer must send what is on screen, not what React last rendered.
 *
 * Found by using the app: typing a message and pressing Enter in the same
 * tick sent nothing at all. The turn never started, the composer cleared
 * itself, and the only sign anything had happened was that no conversation
 * appeared.
 *
 * The cause was a regression from making the draft local to the composer for
 * typing speed: the draft is pushed to the parent on a timer, so `draft` (the
 * state) is a render behind while `draftRef.current` is written synchronously
 * on every keystroke. `trySend` was reading the stale one, which on the first
 * message of a conversation is the empty string.
 *
 * This check reads the source rather than running the component, in the same
 * spirit as the other checks in this folder: the rule it protects is "the
 * send path reads the ref", and that is visible in the text.
 */
import { readFileSync } from 'node:fs';

const source = readFileSync(
  new URL('../src/screens/studio/Composer.tsx', import.meta.url), 'utf8');

const body = source.slice(source.indexOf('const trySend'));
const trySend = body.slice(0, body.indexOf('\n  };') + 5);

if (!trySend.includes('draftRef.current')) {
  throw new Error('trySend must read draftRef.current, not the rendered state');
}

// `const current = draftRef.current` is the only read allowed; a bare `draft`
// anywhere in the send path is the bug coming back. Comments are stripped
// first -- the explanation of the bug names the stale variable, and the first
// version of this check failed on its own prose.
const code = trySend
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\/\/[^\n]*/g, '')
  .replace(/draftRef\.current/g, '');
const stale = /\bdraft\b(?!Ref)(?!\s*:)/.exec(code);
if (stale) {
  throw new Error(
    `trySend still reads the rendered draft ("${stale[0]}"); it is a render behind`);
}

if (!trySend.includes('flushDraft()')) {
  throw new Error('trySend must still flush the pending draft to the parent');
}

console.log('composer-send-draft: ok');
