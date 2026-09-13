#!/usr/bin/env node
// Reads ../../docs/api/sse_events.json (the server's own catalogue) and
// emits src/events.generated.ts: one interface per `stability: core` event,
// CORE_EVENT_TYPES (every core event's non-null `type`) and SCHEMA_VERSION.
//
// `--check` regenerates in memory and diffs against the committed file
// instead of writing it, so `npm run check` fails when the catalogue moved
// and nobody re-ran this script (no `git` dependency: a fresh clone without
// a .git directory, or a dirty tree for unrelated reasons, must still be
// able to run the check).
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const catalogPath = join(here, '..', '..', '..', 'docs', 'api', 'sse_events.json');
const outPath = join(here, '..', 'src', 'events.generated.ts');

const catalog = JSON.parse(readFileSync(catalogPath, 'utf8'));

/** ENVELOPE fields every decoded frame carries (see sse_events.json's own
 *  `envelope` block) — folded, optional, into every generated interface. */
const ENVELOPE = [
  ['sequence', 'number'],
  ['trace_id', 'string'],
  ['step_id', 'string'],
  ['stream_id', 'string'],
  ['schema_version', 'string'],
];

function pascalCase(type) {
  return type
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}

/** A `fields` value → a TS type string. See CONTRATO_SDK_S2.md § S2.3 for
 *  the exact mapping this implements. */
function tsType(spec) {
  if (spec && typeof spec === 'object' && !Array.isArray(spec)) {
    // Nested object: {"question": "string", "multi": "bool?", ...}
    return renderInlineObject(spec);
  }
  const raw = String(spec);
  const optional = raw.endsWith('?');
  const body = optional ? raw.slice(0, -1) : raw;
  return baseType(body);
}

function baseType(body) {
  switch (body) {
    case 'string':
      return 'string';
    case 'bool':
      return 'boolean';
    case 'int':
    case 'number':
      return 'number';
    case 'object':
      return 'Record<string, unknown>';
    case 'any':
      return 'unknown';
    case 'array':
      return 'unknown[]';
    default:
      break;
  }
  // "[{id,label,description?}]" — an array of objects, shape not modelled.
  if (/^\[\{.*\}\]$/.test(body)) return 'unknown[]';
  // "{message, error_class}" — an inline object of bare (string) field names.
  if (/^\{.*\}$/.test(body)) {
    const names = body
      .slice(1, -1)
      .split(',')
      .map((n) => n.trim())
      .filter(Boolean);
    const fields = names
      .map((n) => {
        const opt = n.endsWith('?');
        const name = opt ? n.slice(0, -1) : n;
        return `${name}${opt ? '?' : ''}: string;`;
      })
      .join(' ');
    return `{ ${fields} }`;
  }
  // "pending|confirmed|failed" — a union of string literals.
  if (body.includes('|')) {
    return body
      .split('|')
      .map((p) => `'${p.trim()}'`)
      .join(' | ');
  }
  return 'unknown';
}

function fieldLine(name, spec) {
  const raw = typeof spec === 'string' ? spec : null;
  const optional = raw ? raw.endsWith('?') : false;
  return `  ${name}${optional ? '?' : ''}: ${tsType(spec)};`;
}

function renderInlineObject(fields) {
  const lines = Object.entries(fields).map(([name, spec]) => fieldLine(name, spec));
  return `{\n${lines.map((l) => '  ' + l).join('\n')}\n  }`;
}

function renderEnvelope() {
  return ENVELOPE.map(([name, type]) => `  ${name}?: ${type};`).join('\n');
}

const coreEvents = catalog.events.filter((e) => e.stability === 'core' && e.type !== null);
const coreEventTypes = coreEvents.map((e) => e.type);

const interfaces = coreEvents.map((e) => {
  const name = `${pascalCase(e.type)}Event`;
  const fieldLines = Object.entries(e.fields || {}).map(([n, spec]) => fieldLine(n, spec));
  const body = [`  type: '${e.type}';`, ...fieldLines, renderEnvelope()].join('\n');
  return `/** \`${e.type}\` (${e.stability}). ${(e.note || '').replace(/\*\//g, '*\\/')} */\nexport interface ${name} {\n${body}\n}`;
});

const header = `/**
 * GENERATED — do not edit by hand.
 *
 * Produced by \`sdk/ts/scripts/gen-events.mjs\` from
 * \`docs/api/sse_events.json\` (schema_version ${catalog.schema_version}).
 * Run \`npm run gen:events\` after that file changes; \`npm run check\` fails
 * the build if this file has drifted from it.
 */
`;

const footer = `
export const CORE_EVENT_TYPES = [
${coreEventTypes.map((t) => `  '${t}',`).join('\n')}
] as const;

export type CoreEventType = (typeof CORE_EVENT_TYPES)[number];

export const SCHEMA_VERSION = '${catalog.schema_version}';
`;

const output = header + '\n' + interfaces.join('\n\n') + '\n' + footer;

if (process.argv.includes('--check')) {
  let existing = '';
  try {
    existing = readFileSync(outPath, 'utf8');
  } catch {
    existing = '';
  }
  if (existing !== output) {
    console.error(
      `${outPath} is out of date with docs/api/sse_events.json — run 'npm run gen:events' and commit the result.`,
    );
    process.exit(1);
  }
  process.exit(0);
}

writeFileSync(outPath, output);
console.log(`wrote ${outPath} (${coreEventTypes.length} core event types)`);
