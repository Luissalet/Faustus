// Web-source handling for the Studio transcript: dedupe-by-URL in the
// `apply()` reducer (screens/studio/model.ts), and the favicon helpers in
// adapters/chat.ts (domain extraction, /api/favicon path, and the
// dedupe-by-site + cap-at-N "favicon strip" used by the activity rail's
// collapsed "Thought for N s" summary and its expanded body).
//
// Run by tests/test_studio_guards.py, or by hand:
//   node studio/checks/sources.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

async function bundle(entry, name) {
  const out = join(mkdtempSync(join(tmpdir(), `fs-${name}-`)), `${name}.mjs`);
  await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  return import(pathToFileURL(out).href);
}

const model = await bundle(join(root, 'studio', 'src', 'screens', 'studio', 'model.ts'), 'model-sources');
const chat = await bundle(join(root, 'studio', 'src', 'adapters', 'chat.ts'), 'chat-sources');

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── domainOf / faviconUrlFor ──
{
  assert(chat.domainOf('https://www.marca.com/futbol/x') === 'marca.com', 'domainOf strips www.');
  assert(chat.domainOf('https://as.com/y') === 'as.com', 'domainOf keeps a bare host');
  assert(chat.domainOf('not a url') === '', 'domainOf never throws on garbage, returns empty string');
  assert(chat.faviconUrlFor('marca.com') === '/api/favicon?domain=marca.com', 'faviconUrlFor builds the same-origin proxy path');
  assert(chat.faviconUrlFor('') === '', 'faviconUrlFor of an empty domain is empty (no broken <img src>)');
}

// ── apply(): 'sources' events dedupe by URL, across multiple events ──
{
  let t = model.blankTurn('assistant');
  t = model.apply(t, {
    type: 'sources',
    sources: [
      { title: 'Real Madrid gana', url: 'https://marca.com/a', domain: 'marca.com', favicon: '/api/favicon?domain=marca.com' },
      { title: 'Resultado del clásico', url: 'https://as.com/b', domain: 'as.com', favicon: '/api/favicon?domain=as.com' },
    ],
  });
  assert(t.sources.length === 2, 'first sources event: two sources kept');

  // A second web_search/web_fetch call in the same turn re-emits the event.
  // One URL repeats (must not duplicate), one is new (must be appended).
  t = model.apply(t, {
    type: 'sources',
    sources: [
      { title: 'Real Madrid gana (dup)', url: 'https://marca.com/a', domain: 'marca.com', favicon: '/api/favicon?domain=marca.com' },
      { title: 'Crónica del partido', url: 'https://mundodeportivo.com/c', domain: 'mundodeportivo.com', favicon: '/api/favicon?domain=mundodeportivo.com' },
    ],
  });
  assert(t.sources.length === 3, 'second sources event: repeated URL not duplicated, new URL appended');
  const urls = t.sources.map((s) => s.url);
  assert(new Set(urls).size === urls.length, 'no duplicate URLs across the merged sources list');
  // The FIRST title for a repeated URL wins -- the merge does not overwrite
  // an already-known source's title/favicon on a later, redundant event.
  assert(t.sources.find((s) => s.url === 'https://marca.com/a').title === 'Real Madrid gana', 'first-seen title for a URL is kept, not overwritten');
}

// ── faviconStripEntries: dedupe by domain + cap at max, with an overflow count ──
{
  const many = [
    { title: 'a1', url: 'https://marca.com/1', domain: 'marca.com', favicon: '/api/favicon?domain=marca.com' },
    { title: 'a2', url: 'https://marca.com/2', domain: 'marca.com', favicon: '/api/favicon?domain=marca.com' },
    { title: 'b', url: 'https://as.com/1', domain: 'as.com', favicon: '/api/favicon?domain=as.com' },
    { title: 'c', url: 'https://mundodeportivo.com/1', domain: 'mundodeportivo.com', favicon: '/api/favicon?domain=mundodeportivo.com' },
    { title: 'd', url: 'https://sport.es/1', domain: 'sport.es', favicon: '/api/favicon?domain=sport.es' },
    { title: 'e', url: 'https://espn.com/1', domain: 'espn.com', favicon: '/api/favicon?domain=espn.com' },
    { title: 'f', url: 'https://bbc.com/1', domain: 'bbc.com', favicon: '/api/favicon?domain=bbc.com' },
    { title: 'g', url: 'https://cnn.com/1', domain: 'cnn.com', favicon: '/api/favicon?domain=cnn.com' },
  ];
  const { shown, extra } = chat.faviconStripEntries(many, 6);
  assert(shown.length === 6, 'favicon strip caps at max (6)');
  assert(extra === 1, 'seven distinct sites, six shown -> +1 overflow (marca.com counted once)');
  assert(new Set(shown.map((s) => s.domain)).size === shown.length, 'shown entries have no repeated domain');

  const few = chat.faviconStripEntries(many.slice(0, 2), 6);
  assert(few.shown.length === 1 && few.extra === 0, 'two URLs on the same domain collapse to one icon, no overflow');

  const empty = chat.faviconStripEntries([], 6);
  assert(empty.shown.length === 0 && empty.extra === 0, 'no sources -> nothing to show, no overflow');
}

// ── faviconStripEntries: wider cap for the expanded body (max=12) ──
{
  const nine = Array.from({ length: 9 }, (_, i) => ({
    title: `s${i}`, url: `https://site${i}.example/`, domain: `site${i}.example`,
    favicon: `/api/favicon?domain=site${i}.example`,
  }));
  const { shown, extra } = chat.faviconStripEntries(nine, 12);
  assert(shown.length === 9 && extra === 0, 'a wider cap shows everything when the count is under it');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
} else {
  console.log('\nAll sources checks passed.');
}
