#!/usr/bin/env node
/**
 * Lote 86 follow-up: SourceControlPanel's compact mode, checked by
 * BEHAVIOUR instead of by slicing source text. The old check
 * (`l86-source-control-panel.check.mjs`) used to assert that a source-text
 * slice between `if (compact) {` and the full-mode `return (` did not
 * contain the string `NewRepositoryDialog` — which broke the day compact
 * mode legitimately grew a "Create repository" empty-state action that
 * renders `<NewRepositoryDialog ... />` (closed until clicked). A test
 * asserting on code text instead of what actually shows up in the DOM is
 * exactly the kind of test that breaks on a correct change, so this
 * replaces it: mount the REAL component (esbuild + happy-dom, same
 * pattern `studio/checks/creator-render.check.mjs` and
 * `studio/checks/creator-timeline.check.mjs` use) against a fake `fetch`,
 * and assert on the rendered DOM.
 *
 * Two scenarios:
 *  (a) one repo returned — the compact view shows the branch control, the
 *      changes/commit area and Fetch/Pull/Push/Sync, and none of the
 *      full-mode-only dialogs/buttons (New repository, Create branch,
 *      Publish to GitHub, repository policy) appear anywhere in the DOM.
 *  (b) no repos, with a workspace — the empty state offers "Create
 *      repository"; nothing is open at first; clicking it opens the New
 *      repository dialog.
 *
 * Run: `node studio/checks/l86-source-control-compact.render.check.mjs`
 */
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);

for (const p of [
  'studio/src/screens/source-control/SourceControlPanel.tsx',
  'studio/src/adapters/git.ts',
]) {
  assert.ok(existsSync(path(p)), `missing ${p}`);
}

// Every dialog/button that must stay OUT of the compact DOM — their exact
// testids, read off the dialog files themselves rather than guessed.
const FORBIDDEN_TESTIDS = [
  'new-repo-dialog', // NewRepositoryDialog.tsx
  'create-branch-dialog', // CreateBranchDialog.tsx
  'merge-dialog', // MergeDialog.tsx
  'publish-github-dialog', // PublishToGithubDialog.tsx
  'repo-policy-dialog', // RepoPolicyDialog.tsx
  'repo-header-new-branch',
  'repo-header-merge',
  'repo-header-publish',
  'repo-policy-open',
  'new-repo-open', // the full-mode rail's own "New repository" button
];

const REPO_ONE = {
  id: 'repo1', path: 'D:/w', name: 'w', project_id: 'p1', project_name: 'Project P1',
  root_folder: 'D:/w', parent_repo_id: null, branch: 'main', detached: false,
  head_sha: 'abc123', upstream: 'origin/main', ahead: 0, behind: 0,
  dirty: { staged: 0, unstaged: 0, untracked: 0 },
  user: { name: 'Faustus', email: 'faustus@example.invalid' },
  remotes: [{ name: 'origin', fetch_url: 'git@example.invalid:o/r.git', push_url: 'git@example.invalid:o/r.git' }],
  identity: null,
};

const STATUS_CLEAN = {
  branch: 'main', detached: false, ahead: 0, behind: 0, upstream: 'origin/main',
  staged: [], unstaged: [], untracked: [], conflicts: [],
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** Builds a fake fetch for one scenario; `calls` collects every request made. */
function makeFetch(scenario, calls) {
  return async (url, init = {}) => {
    const u = String(url);
    const method = init.method || 'GET';
    calls.push({ url: u, method });

    if (u.startsWith('/api/git/repos?')) {
      if (scenario === 'one-repo') return jsonResponse({ repos: [REPO_ONE], git_version: '2.44.0' });
      return jsonResponse({ repos: [], git_version: '2.44.0' });
    }
    if (u.includes('/api/git/repos/repo1/status')) return jsonResponse(STATUS_CLEAN);
    if (u.includes('/api/git/repos/repo1/log')) return jsonResponse({ commits: [], next_cursor: null });
    if (u.includes('/board/summary')) return jsonResponse({ detail: 'no board in this fixture' }, 404);
    // NewRepositoryDialog's own on-open lookups (scenario b, once opened).
    if (u.startsWith('/api/git/folders')) return jsonResponse({ folders: [] });
    if (u.startsWith('/api/git/identities')) return jsonResponse({ identities: [], ssh_config_path: '', ssh_dir: '' });
    if (u.startsWith('/api/git/github/accounts')) return jsonResponse({ available: false, version: null, accounts: [] });
    return jsonResponse({ detail: 'unhandled in fixture: ' + u }, 404);
  };
}

async function main() {
  const { build: esbuildBuild } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);

  const harnessSrc = `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { MemoryRouter } from 'react-router';
    import { SourceControlPanel } from './studio/src/screens/source-control/SourceControlPanel';

    export function mount(containerId) {
      const root = createRoot(document.getElementById(containerId));
      root.render(
        React.createElement(MemoryRouter, null,
          React.createElement(SourceControlPanel, { compact: true, workspace: 'D:/w', projectId: 'p1' }),
        ),
      );
    }
  `;

  const bundle = await esbuildBuild({
    stdin: { contents: harnessSrc, resolveDir: root, loader: 'tsx', sourcefile: 'harness.tsx' },
    bundle: true,
    format: 'esm',
    platform: 'browser',
    write: false,
    logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    loader: { '.tsx': 'tsx', '.ts': 'ts', '.css': 'empty' },
  });
  const code = bundle.outputFiles[0].text;

  const tmp = mkdtempSync(join(tmpdir(), 'sc-compact-render-check-'));
  const bundlePath = join(tmp, 'sc-compact-harness.mjs');
  writeFileSync(bundlePath, code, 'utf8');

  const window = new Window({ url: 'https://studio.faustus.invalid/' });
  const document = window.document;
  document.body.innerHTML = '<div id="root-a"></div><div id="root-b"></div><div id="fs-overlay-root"></div>';

  for (const key of [
    'window', 'document', 'HTMLElement', 'SVGElement', 'Node', 'NodeFilter', 'Element', 'Event', 'CustomEvent',
    'MouseEvent', 'localStorage', 'sessionStorage', 'MutationObserver', 'getComputedStyle',
    'DocumentFragment', 'requestAnimationFrame', 'cancelAnimationFrame', 'Response', 'Headers',
    'HTMLInputElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'HTMLAnchorElement',
    'HTMLButtonElement', 'FocusEvent', 'KeyboardEvent', 'PointerEvent',
  ]) {
    if (key in window) {
      try { globalThis[key] = window[key]; } catch { /* a few are getter-only in Node 22 */ }
    }
  }

  const mod = await import(pathToFileURL(bundlePath).toString());

  // ── (a) one repo: the compact working view, none of the full-mode chrome ──
  const callsA = [];
  globalThis.fetch = makeFetch('one-repo', callsA);
  mod.mount('root-a');
  await new Promise((r) => setTimeout(r, 60));

  const rootA = document.getElementById('root-a');
  assert.ok(rootA.querySelector('[data-testid="source-control-panel-compact"]'), 'compact panel must mount');
  assert.ok(rootA.querySelector('[data-testid="repo-branch-chip"]'), 'the branch control (BranchPopover) must be shown');
  assert.ok(rootA.querySelector('[data-testid="commit-message"]'), 'the changes/commit area (ChangesPane) must be shown');
  for (const testid of ['repo-header-fetch', 'repo-header-pull', 'repo-header-push', 'repo-header-sync']) {
    assert.ok(rootA.querySelector(`[data-testid="${testid}"]`), `the ${testid} button must be shown`);
  }

  // No dialog is open, anywhere in the document (Radix does not render a
  // closed Dialog's Content to the DOM at all, so this is a real check,
  // not just "nothing with role=dialog happens to be visible").
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 0, 'no dialog must be open in compact mode with a repo selected');
  for (const testid of FORBIDDEN_TESTIDS) {
    assert.equal(document.querySelectorAll(`[data-testid="${testid}"]`).length, 0, `${testid} must not appear anywhere in the DOM in compact mode`);
  }
  // Belt-and-braces: none of the full-mode-only button labels appear as text either.
  for (const label of ['New repository', 'New branch', 'Merge…', 'Publish to GitHub', 'Agent & this repository']) {
    assert.ok(!document.body.textContent.includes(label), `"${label}" must not appear in compact mode`);
  }

  // ── (b) no repos, a workspace: empty state offers "Create repository",
  // nothing open yet, clicking it opens the New repository dialog ──
  const callsB = [];
  globalThis.fetch = makeFetch('empty', callsB);
  mod.mount('root-b');
  await new Promise((r) => setTimeout(r, 60));

  const rootB = document.getElementById('root-b');
  assert.ok(rootB.querySelector('[data-testid="source-control-panel-compact"]'), 'compact panel must mount even with no repos');
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 0, 'no dialog must be open before the empty-state action is clicked');
  assert.equal(document.querySelectorAll('[data-testid="new-repo-dialog"]').length, 0, 'the New repository dialog must not be in the DOM before it is opened');

  const createBtn = rootB.querySelector('[data-testid="btn-create-repository"]');
  assert.ok(createBtn, 'the empty state must offer a "Create repository" action when a workspace is given');
  assert.ok(rootB.textContent.includes('Create repository'), 'the action must be labelled "Create repository"');

  createBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 60));

  const dialog = document.querySelector('[data-testid="new-repo-dialog"]');
  assert.ok(dialog, 'clicking "Create repository" must open the New repository dialog');
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1, 'exactly one dialog must now be open');
  assert.ok(document.body.textContent.includes('New repository'), 'the open dialog must show the New repository title');

  rmSync(tmp, { recursive: true, force: true });
  console.log('ok l86-source-control-compact (' + (callsA.length + callsB.length) + ' fetch calls)');
  // The panel's own polling intervals (window.setInterval, POLL_MS) are
  // never cleared since the roots are never unmounted here — force the
  // process closed rather than let happy-dom's real timers hang node.
  process.exit(0);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
