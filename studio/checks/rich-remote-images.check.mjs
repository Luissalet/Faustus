// A remote picture in a reply waits for a click: loading it on sight sends
// whatever its URL carries to that host (an injected page turns the model
// into a beacon). Same-origin, data: and blob: load at once; a host the
// person allowed loads at once for the rest of the tab. Static assertions.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const rich = readFileSync(new URL('../src/screens/rich.tsx', import.meta.url), 'utf8');
assert.match(rich, /function RichImage\(/, 'RichImage exists');
assert.match(rich, /case 'image':\s*out\.push\(<RichImage /, 'markdown images go through RichImage');
assert.doesNotMatch(rich, /case 'image':\s*out\.push\(<img /, 'no image loads straight from the markdown');
assert.match(rich, /url\.origin === window\.location\.origin\) return null/, 'same-origin loads at once');
assert.match(rich, /allowedImageHosts\.add\(host\)/, 'one click allows the host for the tab');
assert.match(rich, /data-testid="rich-remote-image"/, 'the placeholder');
console.log('rich-remote-images: ALL OK');
