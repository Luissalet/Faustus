import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-vitals-i18n-'));
try {
  const out = join(dir, 'check.mjs');
  await build({ stdin: { contents: "export * from './studio/src/lib/vitals-i18n'; export { setLang } from './studio/src/i18n';", resolveDir: root }, bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
  const m = await import(pathToFileURL(out).href);
  const health = { score: 87, grade: 'B', collected: true, components: [
    { name: 'vram', label: 'A changed server label', value: '94% free', state: 'ok' },
    { name: 'host', value: '51% used', state: 'ok' },
    { name: 'disk', value: '82.5 GB free (9%)', state: 'warn' },
    { name: 'runners', value: 'none', state: 'ok' },
    { name: 'custom', label: 'Custom signal', value: 'unknown: 1.2.3% free', state: 'no_data' },
  ] };
  const original = JSON.stringify(health);
  m.setLang('es', { persist: false });
  const es = m.healthForDisplay(health);
  assert.equal(es.components[0].label, 'VRAM disponible');
  assert.equal(es.components[0].value, '94% libre');
  assert.equal(es.components[1].value, '51% en uso');
  assert.equal(es.components[2].value, '82,5 GB libres (9%)');
  assert.equal(es.components[3].value, 'ninguno');
  assert.equal(es.components[4].label, 'Custom signal');
  assert.equal(es.components[4].value, 'unknown: 1.2.3% free');
  assert.equal(es.components[4].state, 'no_data');
  assert.equal(JSON.stringify(health), original);
  assert.equal(m.vitalsText('1.2.3% free'), '1.2.3% free');
  assert.equal(m.vitalsText('reachable · 1 model(s) loaded'), 'accesible · 1 modelo cargado');
  assert.equal(m.vitalsText('reachable · 2 model(s) loaded'), 'accesible · 2 modelos cargados');
  assert.equal(m.vitalsText('1000 card(s)'), '1000 tarjetas');
  assert.equal(m.vitalsText('1/6 endpoint(s) reachable.'), 'Responden 1 de 6 servidores de modelos.');
  assert.equal(m.vitalsText('ConnectionRefusedError: Unreachable (connection refused).'), 'ConnectionRefusedError: No accesible (conexión rechazada).');
  assert.equal(m.vitalsText('ConnectionError: 1 of 4 servers failing (qa-server); qa-server: opaque error'), 'ConnectionError: Fallan 1 de 4 servidores (qa-server); qa-server: opaque error');
  assert.equal(m.vitalsText('Do not translate https://qa.invalid/status?x=1'), 'Do not translate https://qa.invalid/status?x=1');
  assert.equal(m.vitalsText('Error: '.repeat(1000)), 'Error: '.repeat(1000));
  const item = { id: 'providers', label: 'Old label', state: 'degraded', cause: '1/6 endpoint(s) reachable.', affects: ['agent runs', 'unknown feature'], hint: { es: 'Pista original', en: 'Original hint', command: 'qa --help' }, since: '2026-10-10', lastChecked: '', latencyMs: 0 };
  const report = { overall: 'degraded', generatedAt: '', items: [item], problems: [item], unconfigured: 0 };
  const reportBefore = JSON.stringify(report);
  const translated = m.degradationForDisplay(report);
  assert.equal(translated.problems[0].cause, 'Responden 1 de 6 servidores de modelos.');
  assert.deepEqual(translated.problems[0].affects, ['ejecuciones de agentes', 'unknown feature']);
  assert.equal(translated.problems[0].hint.command, 'qa --help');
  assert.equal(translated.problems[0].since, item.since);
  assert.equal(JSON.stringify(report), reportBefore);
  m.setLang('en', { persist: false });
  assert.equal(m.healthForDisplay(health).components[2].value, '82.5 GB free (9%)');
  assert.equal(m.degradationForDisplay(report).problems[0].cause, item.cause);
  for (const name of Object.keys(m.HEALTH_LABEL)) {
    m.setLang('es', { persist: false });
    assert.notEqual(m.healthForDisplay({ components: [{ name }], score: 0 }).components[0].label, m.HEALTH_LABEL[name]);
  }
  console.log('ALL OK: names, locale, singular/plural, unknown diagnostics, immutable raw data, language switch');
} finally { rmSync(dir, { recursive: true, force: true }); }
