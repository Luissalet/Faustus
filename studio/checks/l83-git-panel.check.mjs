// Lote 83 (OBJ-4) — SSH identity chip, "New repository" and the agent's
// git policy's pure logic (studio/src/adapters/git.ts): repo-name
// validation, the https/ssh remote-URL rewrite onto an ssh host alias, the
// push-toggle's disabled rule, and the identity chip's label. None of this
// touches the DOM or fetch, so it is exercised here directly rather than
// through a browser.
//
// Bundled with esbuild on the fly; run by tests/test_l83_git_panel_js.py,
// or by hand:
//   node studio/checks/l83-git-panel.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-git-panel-'));

async function load(rel, name) {
  const out = join(dir, name);
  await build({
    entryPoints: [join(root, 'studio', 'src', rel)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    logLevel: 'silent',
  });
  return import(pathToFileURL(out).href);
}

const g = await load(join('adapters', 'git.ts'), 'git.mjs');

let failed = 0;
const assert = (condition, message) => {
  if (!condition) {
    failed += 1;
    console.error('FAIL:', message);
  } else console.log('ok:', message);
};

// ── isValidRepoName: [A-Za-z0-9._-]+, non-empty ──
{
  assert(g.isValidRepoName('faustus') === true, 'plain letters are valid');
  assert(g.isValidRepoName('my-project_2.0') === true, 'dash, underscore and dot are allowed');
  assert(g.isValidRepoName('') === false, 'empty name is invalid');
  assert(g.isValidRepoName('has space') === false, 'a space is rejected');
  assert(g.isValidRepoName('a/b') === false, 'a slash is rejected — it would escape the parent folder');
  assert(g.isValidRepoName('yes!') === false, 'punctuation outside the allowed set is rejected');
}

// ── rewriteRemoteToAlias: https and ssh remotes -> git@<alias>:o/r.git ──
{
  assert(
    g.rewriteRemoteToAlias('https://github.com/Luissalet/Faustus.git', 'github-work') === 'git@github-work:Luissalet/Faustus.git',
    'an https remote rewrites onto the given alias',
  );
  assert(
    g.rewriteRemoteToAlias('https://github.com/owner/repo', 'alias') === 'git@alias:owner/repo.git',
    'an https remote with no trailing .git still rewrites correctly',
  );
  assert(
    g.rewriteRemoteToAlias('git@Luissalet:Luissalet/Faustus.git', 'github-lsaletec') === 'git@github-lsaletec:Luissalet/Faustus.git',
    'an ssh remote already on one alias rewrites onto a different one — only the host alias changes, the owner/repo path does not',
  );
  assert(g.rewriteRemoteToAlias('not a remote at all', 'alias') === null, 'an unrecognized remote shape previews as null, never a wrong guess');
  assert(g.rewriteRemoteToAlias('', 'alias') === null, 'an empty remote is null');
}

// ── pushToggleDisabled: push cannot outlive commit ──
{
  assert(g.pushToggleDisabled(false) === true, 'push is disabled while commit is off');
  assert(g.pushToggleDisabled(true) === false, 'push is enabled once commit is on');
}

// ── identityChipLabel: t()-key + values, never a hardcoded rendered string ──
{
  const withLogin = g.identityChipLabel({ label: 'Luissalet', github_login: 'Luissalet' });
  assert(withLogin.key === 'SSH: {label} (login: {login})', 'an identity with a cached login uses the login-aware key');
  assert(withLogin.values.label === 'Luissalet' && withLogin.values.login === 'Luissalet', 'label and login both interpolate');

  const noLogin = g.identityChipLabel({ label: 'github-work', github_login: null });
  assert(noLogin.key === 'SSH: {label}', 'an identity with no cached login uses the plain key');
  assert(noLogin.values.label === 'github-work', 'label still interpolates without a login');

  const none = g.identityChipLabel(null);
  assert(none.key === 'No SSH identity — using https/none' && none.values === undefined, 'no active identity (https or no match) uses the fixed no-identity key');
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log('\nGit panel (lote 83): all checks passed');
