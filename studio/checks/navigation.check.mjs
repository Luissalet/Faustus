import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { Window } from 'happy-dom';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const temporary = mkdtempSync(join(tmpdir(), 'faustus-navigation-'));
const browser = new Window({ url: 'http://127.0.0.1/' });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'MutationObserver', 'getComputedStyle']) Object.defineProperty(globalThis, key, { configurable: true, value: key === 'getComputedStyle' ? browser.getComputedStyle.bind(browser) : browser[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.fetch = async () => ({ ok: true, json: async () => ({ creator_enabled: false }) });

try {
  const outfile = join(temporary, 'test.mjs');
  await build({ stdin: { contents: `
    export * from './studio/src/shell/routes';
    export * from './studio/src/shell/toolPlacement';
    export * from './studio/src/screens/settings/SettingsNavigation';
    export * from './studio/src/screens/studio/commands';
    export { createElement, act, useState } from 'react';
    export { createRoot } from 'react-dom/client';
    export { MemoryRouter } from 'react-router';
  `, resolveDir: root, loader: 'tsx' }, bundle: true, platform: 'node', format: 'esm', outfile, logLevel: 'silent' });
  const m = await import(pathToFileURL(outfile));
  assert.deepEqual(Object.keys(m.TOOL_PLACEMENT).sort(), m.TOOLS.map(tool => tool.path).sort(), 'every former Tool has a working home');
  assert.equal(m.toolsIn('project').filter(tool => tool.path === '/source-control').length, 1);
  assert.equal(m.toolHref('/source-control', 'project a'), '/projects/project%20a?tab=repos');
  assert.equal(m.toolHref('/alternatives', 'project a'), '/alternatives?project=project+a');
  assert.equal(m.toolContext('/projects/abc'), 'project');
  assert.equal(m.toolContext('/extraction'), 'library');
  assert.equal(m.toolContext('/completion'), 'activity');
  assert.equal(m.toolsIn('library').some(tool => tool.path === '/creator'), false);
  assert.equal(m.toolsIn('library', true).some(tool => tool.path === '/creator'), true);
  assert.equal(m.resolveCommand('research', 'a question').command.route, undefined, 'research stays in the conversation');
  for (const name of ['extract', 'extraction', 'git', 'context', 'skills', 'hoards']) assert.ok(m.resolveCommand(name, ''), name);
  assert.equal(m.DESTINATIONS.some(item => item.path === '/source-control'), false);
  assert.equal(m.DESTINATIONS.length, 7);
  assert.equal(m.MOBILE_DESTINATIONS.length, 4, 'the fifth mobile control opens search/navigation');

  const settingsSource = readFileSync(join(root, 'studio/src/screens/Settings.tsx'), 'utf8');
  const sectionKeys = [...settingsSource.slice(settingsSource.indexOf('const SECTIONS:'), settingsSource.indexOf('/* ── Models:')).matchAll(/key: '([^']+)'/g)].map(match => match[1]);
  const grouped = m.SETTINGS_GROUPS.flatMap(group => group.sections);
  assert.deepEqual([...grouped].sort(), [...sectionKeys].sort(), 'every settings section occurs exactly once');
  assert.equal(new Set(grouped).size, grouped.length);
  assert.ok(m.settingMatches('defaults', 'Default AI', 'temperatura'));
  assert.ok(m.settingMatches('voice', 'Voice', 'dictado'));
  assert.ok(m.settingMatches('repositories', 'Repositories', 'git'));

  const sections = sectionKeys.map(key => ({ key, label: key === 'voice' ? 'Voice' : key }));
  let selected = 'general';
  function Harness() {
    const [section, setSection] = m.useState('general');
    return m.createElement(m.MemoryRouter, null, m.createElement(m.SettingsNavigation, { sections, section, onChange: value => { selected = value; setSection(value); } }));
  }
  const host = document.createElement('div'); document.body.append(host);
  const renderer = m.createRoot(host);
  await m.act(async () => renderer.render(m.createElement(Harness)));
  assert.equal(host.querySelectorAll('[data-testid^="settings-group-"]').length, 5);
  assert.equal(host.querySelector('[data-testid="settings-section-local"]'), null, 'model infrastructure does not crowd general settings');
  await m.act(async () => host.querySelector('[data-testid="settings-group-ai"]').click());
  assert.equal(selected, 'models');
  assert.ok(host.querySelector('[data-testid="settings-section-local"]'));
  assert.equal(host.querySelector('[data-testid="settings-section-shortcuts"]'), null);
  await m.act(async () => renderer.unmount());

  const shell = readFileSync(join(root, 'studio/src/shell/AppShell.tsx'), 'utf8');
  assert.ok(!shell.includes('tools.map('), 'no permanent tool inventory in the sidebar');
  assert.ok(!shell.includes('useBadges'), 'sidebar does not poll every repository');
  assert.ok(shell.includes('nav-more') && shell.includes('nav-search'), 'mouse and mobile can reach all destinations');
  console.log('ALL OK: navigation coverage, project scope, research, settings groups and live settings navigation');
} finally {
  await browser.happyDOM.abort();
  assert.equal(dirname(resolve(temporary)), resolve(tmpdir()));
  assert.ok(temporary.startsWith(join(tmpdir(), 'faustus-navigation-')));
  rmSync(temporary, { recursive: true, force: true });
}
process.exit(0);
