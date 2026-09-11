// CMP-04 (CONTRATO_CMP_W2.md, W2-D) — the knowledge neighborhood in Studio:
// adapters/knowledge.ts (GET /api/projects/{id}/knowledge/neighborhood),
// a compact "Contexto usado (n) — por qué" card in Transcript.tsx fed by the
// new `context_receipts` SSE event (adapters/chat.ts), and "Vecindario de un
// fichero" in Project.tsx's Context tab. Static source inspection, like
// l99-studio-topology.check.mjs: this is JSX wiring across several large
// screens, not pure logic a bundled import can exercise on its own.
//
// Run by tests/test_cmp04_knowledge_js.py, or by hand:
//   node studio/checks/knowledge.check.mjs
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const path = (p) => join(root, p);
const read = (p) => readFileSync(path(p), 'utf8').replace(/\r\n/g, '\n');

// ── adapters/knowledge.ts: the one read, correctly shaped ──
{
  const p = 'studio/src/adapters/knowledge.ts';
  assert.ok(existsSync(path(p)), `missing ${p}`);
  const src = read(p);
  assert.ok(src.includes('export async function getNeighborhood'), 'must export getNeighborhood');
  assert.ok(src.includes('/knowledge/neighborhood'), 'getNeighborhood must call .../knowledge/neighborhood');
  assert.ok(src.includes("import { getJson } from './api'"), 'must read through adapters/api.ts, not fetch() directly');
  assert.ok(!/\bfetch\(/.test(src), 'adapters/knowledge.ts must not call fetch() directly');
  for (const exported of ['export interface NeighborNode', 'export interface NeighborEdge', 'export interface Neighborhood', 'export type NeighborRelation', 'export type NeighborNodeType']) {
    assert.ok(src.includes(exported), `knowledge.ts must export: ${exported}`);
  }
  // relation is read straight off the server, never invented client-side.
  assert.ok(src.includes("'declared', 'located', 'verified'"), 'relation must stay exactly the server\'s three values');
}

// ── adapters/chat.ts: the context_receipts event, decoded ──
{
  const p = 'studio/src/adapters/chat.ts';
  const src = read(p);
  assert.ok(src.includes('export interface ContextReceipt'), 'chat.ts must export a ContextReceipt type');
  assert.ok(/type:\s*'context_receipts';\s*receipts:\s*ContextReceipt\[\]/.test(src), 'ChatEvent must gain a context_receipts variant');
  assert.ok(src.includes("case 'context_receipts':"), 'decode() must handle the context_receipts SSE event');
}

// ── screens/studio/Transcript.tsx: the compact, collapsible card ──
{
  const p = 'studio/src/screens/studio/Transcript.tsx';
  const src = read(p);
  assert.ok(src.includes('function ContextReceiptCard'), 'Transcript.tsx must define ContextReceiptCard');
  assert.ok(src.includes('<ContextReceiptCard turn={turn} />'), 'ContextReceiptCard must be rendered in the assistant turn');
  assert.ok(/t\('Context used \(\{n\}\) — why'/.test(src), 'the card title must be the exact contracted copy');
  assert.ok(src.includes("import { fetchCompactionEvent") && src.includes('type ContextReceipt'), 'Transcript.tsx must import the ContextReceipt type from adapters/chat');
  // Collapsed by default: a plain <details>, never forced open.
  const cardBody = src.slice(src.indexOf('function ContextReceiptCard'), src.indexOf('function ContextReceiptCard') + 1800);
  assert.ok(cardBody.includes('<details') && !cardBody.includes('<details open'), 'the card must be a collapsed <details>, not forced open');
}

// ── screens/Project.tsx: "Vecindario de un fichero" in the Context tab ──
{
  const p = 'studio/src/screens/Project.tsx';
  const src = read(p);
  assert.ok(src.includes('function FileNeighborhood'), 'Project.tsx must define FileNeighborhood');
  assert.ok(src.includes("from '../adapters/knowledge'") && src.includes('getNeighborhood'), 'FileNeighborhood must read through adapters/knowledge.ts, not a fetch of its own');
  assert.ok(!/\bfetch\(/.test(src), 'Project.tsx must not call fetch() directly — adapters/knowledge.ts already does');
  // Scoped to the Context tab only, never rendered elsewhere.
  const contextoTab = src.slice(src.indexOf("tab === 'contexto'"), src.indexOf("tab === 'repos'"));
  assert.ok(contextoTab.includes('<FileNeighborhood'), '<FileNeighborhood> must be rendered inside the contexto tab');
  const otherTabs = src.slice(0, src.indexOf("tab === 'contexto'")) + src.slice(src.indexOf("tab === 'repos'"));
  assert.ok(!otherTabs.includes('<FileNeighborhood'), 'FileNeighborhood must not be rendered outside the Context tab');
  for (const field of ['What it must satisfy', 'Decisions that condition it', 'Tests that cover it', 'Out of date']) {
    assert.ok(src.includes(field), `FileNeighborhood must group results under: ${field}`);
  }
}

// ── i18n: every new string this lot introduces has a Spanish row (the
// repo-wide gate is `python3 scripts/i18n_es.py --check`; this is a
// narrower, lot-scoped tripwire so this check fails on its own regression
// even while an unrelated lot's keys are still missing). ──
{
  const tsv = read('docs/ui/i18n/es.tsv');
  const keys = new Set(tsv.split('\n').filter((l) => l.includes('\t')).map((l) => l.split('\t')[0]));
  const mustHave = [
    'Context used ({n}) — why', 'Open source', 'File neighborhood', 'File path',
    'Looking up…', 'Could not load the neighborhood.', 'What it must satisfy',
    'No requirements linked to this file yet.', 'Decisions that condition it',
    'No decisions found for this file.', 'Tests that cover it',
    'No tests found for this file.', 'Nothing looks out of date.',
    'Requirement', 'Test file', 'Run record',
  ];
  for (const key of mustHave) {
    assert.ok(keys.has(key), `docs/ui/i18n/es.tsv is missing a Spanish row for: ${key}`);
  }
}

console.log('ok knowledge');
