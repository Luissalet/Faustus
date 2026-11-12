import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const tmp = mkdtempSync(join(tmpdir(), 'status-pulse-'));
try {
  const out = join(tmp, 'pulse.mjs');
  await build({ entryPoints: ['studio/src/shell/pulseState.ts'], bundle: true, format: 'esm', outfile: out });
  const { pulseView, openSession } = await import(pathToFileURL(out).href);
  const snapshot = (patch = {}) => ({ running: [], awaiting: [], queued: {}, ...patch });
  const view = (patch = {}, open = 'open') => pulseView(snapshot(patch), open);
  assert.deepEqual(view(), { state: 'idle', waiting: false });
  assert.equal(view({ running: ['open'] }).state, 'inferring');
  assert.equal(view({ running: ['other'] }).state, 'background');
  assert.equal(view({ running: ['open', 'other'] }).state, 'inferring');
  assert.deepEqual(view({ running: ['open'], awaiting: ['open'] }), { state: 'idle', waiting: true });
  assert.deepEqual(view({ running: ['open'], queued: { open: 2 } }), { state: 'idle', waiting: true });
  assert.equal(view({ running: ['open'], queued: { open: 0 } }).state, 'inferring');
  assert.equal(view({ running: ['other'], queued: { other: 1 } }).state, 'idle');
  assert.equal(view({ running: ['other'], awaiting: ['other'] }).state, 'idle');
  assert.equal(view({ queued: { other: 0 } }).state, 'idle');
  assert.equal(view({ workers: { child: { parent: 'open' } } }).state, 'background');
  assert.equal(view({ workers: { open: {} } }).state, 'idle');
  assert.equal(view({ backgroundJobs: [{ active: false }] }).state, 'idle');
  assert.equal(view({ backgroundJobs: [{ active: true }] }).state, 'background');
  assert.equal(view({ activeAutomations: 1 }).state, 'background');
  assert.equal(view({ agoraWorkers: 3 }).state, 'background');
  assert.equal(view({ running: ['open'] }, null).state, 'background');
  assert.equal(view({ awaiting: ['open'], agoraWorkers: 1 }).state, 'background');
  assert.equal(view({ running: ['open'], details: { open: { phase: 'tool' } } }).state, 'idle');
  assert.equal(view({ running: ['open'], details: { open: { phase: 'thinking' } } }).state, 'inferring');
  assert.equal(openSession('/studio', '?s=one%20two'), 'one two');
  assert.equal(openSession('/', '?s=one'), 'one');
  assert.equal(openSession('/settings', '?s=one'), null);
  assert.equal(openSession('/studio', '?s='), null);
  assert.equal(openSession('/studio', '?p=group'), null);
  assert.equal(openSession('/studio/', '?agora=group&s=one'), 'group');
  console.log('26 status pulse checks passed');
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
