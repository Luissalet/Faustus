const {test} = require('node:test');
const assert = require('node:assert/strict');
const {mkdtempSync, rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');

// Requiring app-shell.cjs here must NOT pull in Electron: `require.main` is
// this test file, not app-shell.cjs, so the `if (require.main === module)`
// block at the bottom of app-shell.cjs never runs and `require('electron')`
// is never reached — exactly what lets `node --test` exercise the shell's
// pure logic in a container with no `desktop/node_modules/electron`.
const shell = require('./app-shell.cjs');

test('argv parsing: --key=value flags, unknown/malformed ones ignored', () => {
  const args = shell.parseArgs([
    '--url=http://127.0.0.1:8767/ui',
    '--title=Sculptor\'s Hoard',
    '--icon=/abs/path/icon.png',
    '--slug=sculptors-abc123',
  ]);
  assert.deepEqual(args, {
    url: 'http://127.0.0.1:8767/ui',
    title: "Sculptor's Hoard",
    icon: '/abs/path/icon.png',
    slug: 'sculptors-abc123',
  });
});

test('argv parsing: missing flags default sensibly, unrecognised flags ignored', () => {
  const args = shell.parseArgs(['--slug=only-slug', '--bogus', '--weird=', 'not-a-flag']);
  assert.equal(args.slug, 'only-slug');
  assert.equal(args.url, null);
  assert.equal(args.title, 'App');
  assert.equal(args.icon, null);
});

test('argv parsing: an empty --title= keeps the default rather than going blank', () => {
  const args = shell.parseArgs(['--title=', '--slug=x']);
  assert.equal(args.title, 'App');
});

test('argv parsing: values may contain "=" themselves (e.g. a query string)', () => {
  const args = shell.parseArgs(['--url=http://127.0.0.1:9/x?a=b&c=d']);
  assert.equal(args.url, 'http://127.0.0.1:9/x?a=b&c=d');
});

test('per-slug profile dir and window-state file are isolated per app', () => {
  const a = shell.profileDir('app-a');
  const b = shell.profileDir('app-b');
  assert.notEqual(a, b);
  assert.ok(a.endsWith(join('data', 'desktop-profile-apps', 'app-a')));
  assert.equal(shell.windowStateFile('app-a'), join(a, 'window.json'));
});

test('profileDir stringifies a non-string slug rather than throwing', () => {
  assert.equal(shell.profileDir(42), shell.profileDir('42'));
});

test('loadWindowState: no file yet -> the 1280x860 default, no throw', () => {
  const dir = mkdtempSync(join(tmpdir(), 'app-shell-test-'));
  const fakeRoot = join(dir, 'root');
  // profileDir() is derived from the module's own `root`, which we cannot
  // rebind without Electron machinery, so this test drives
  // loadWindowState/saveWindowState through a slug scoped to a throwaway
  // temp dir name instead — profileDir() always resolves under the real
  // repo's data/ dir, so we only assert the DEFAULT_BOUNDS fallback here,
  // never touching the real repo's data/ directory as a side effect.
  const slug = 'nonexistent-app-shell-test-slug-' + Date.now();
  const state = shell.loadWindowState(slug);
  assert.deepEqual(state, shell.DEFAULT_BOUNDS);
  rmSync(dir, {recursive: true, force: true});
});

test('saveWindowState then loadWindowState round-trips bounds and maximized', () => {
  const slug = 'app-shell-roundtrip-test-' + Date.now();
  shell.saveWindowState(slug, {width: 1000, height: 700, x: 50, y: 60, maximized: true});
  const state = shell.loadWindowState(slug);
  assert.equal(state.width, 1000);
  assert.equal(state.height, 700);
  assert.equal(state.x, 50);
  assert.equal(state.y, 60);
  assert.equal(state.maximized, true);
  // cleanup: remove what this test wrote under the real repo's data/ dir.
  rmSync(shell.profileDir(slug), {recursive: true, force: true});
});

test('loadWindowState ignores a too-small saved size and falls back to defaults', () => {
  const slug = 'app-shell-tiny-test-' + Date.now();
  shell.saveWindowState(slug, {width: 10, height: 10, maximized: false});
  const state = shell.loadWindowState(slug);
  assert.equal(state.width, shell.DEFAULT_BOUNDS.width);
  assert.equal(state.height, shell.DEFAULT_BOUNDS.height);
  rmSync(shell.profileDir(slug), {recursive: true, force: true});
});

test('targetOrigin extracts the origin, or null for an unparsable URL', () => {
  assert.equal(shell.targetOrigin('http://127.0.0.1:8767/ui?x=1'), 'http://127.0.0.1:8767');
  assert.equal(shell.targetOrigin('not a url'), null);
});

test('navigationDecision: same-origin stays, http(s) elsewhere goes external, anything else is blocked', () => {
  const origin = 'http://127.0.0.1:8767';
  assert.equal(shell.navigationDecision('http://127.0.0.1:8767/other-page', origin), 'stay');
  assert.equal(shell.navigationDecision('https://example.com', origin), 'external');
  assert.equal(shell.navigationDecision('file:///etc/passwd', origin), 'block');
  assert.equal(shell.navigationDecision('javascript:alert(1)', origin), 'block');
  // Not a valid URL at all (an out-of-range "port" of "8767.evil.test") —
  // neither same-origin nor a recognised external scheme, so it is blocked
  // rather than silently treated as either.
  assert.equal(shell.navigationDecision('http://127.0.0.1:8767.evil.test', origin), 'block');
});

test('splashHtml embeds and escapes the title, stays a self-contained data: URL', () => {
  const html = shell.splashHtml('<script>x</script>');
  assert.ok(html.startsWith('data:text/html;charset=utf-8,'));
  const decoded = decodeURIComponent(html.slice('data:text/html;charset=utf-8,'.length));
  assert.ok(!decoded.includes('<script>x</script>'));
  assert.ok(decoded.includes('&lt;script&gt;'));
});
